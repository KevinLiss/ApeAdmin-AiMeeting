"""AI 会议助手插件——业务服务层。

核心工作流：
开始会议进行录音（15 秒准实时切片上传）→ 语音转写（faster-whisper，句级时间戳）
→ LLM 总结 1 句话 → 结构化会议纪要输出（结论、讨论要点、决议、遗留问题）
→ 会后说话人分离（sherpa-onnx 声纹聚类 + 时间戳对齐，虚拟名称可编辑）
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai.agent import chat_non_stream
from src.crud.ai import crud_ai_provider
from src.plugins.builtin.aimeeting.models import (
    AimeetingMeeting,
    AimeetingMinuteRecord,
    AimeetingMinutes,
    AimeetingSpeaker,
    TranscriptStatus,
)

# ── 路径配置 ─────────────────────────────────────────────────────────────
# 录音文件存储目录（相对 backend 根目录，可被配置覆盖）
BACKEND_DIR = Path(__file__).resolve().parents[4]  # backend/
AUDIO_STORE_DIR = Path(os.environ.get("AIMEETING_AUDIO_DIR", BACKEND_DIR / "storage" / "aimeeting" / "audio"))
MODEL_DIR = Path(os.environ.get("AIMEETING_WHISPER_MODEL", BACKEND_DIR / "models" / "faster-whisper-small"))
# 说话人分离模型（sherpa-onnx 官方 pyannote 分段 + Wespeaker 声纹，ONNX）
DIARIZATION_DIR = Path(
    os.environ.get("AIMEETING_DIARIZATION_MODEL", BACKEND_DIR / "models" / "sherpa-diarization")
)

# ── 转写模型（懒加载单例）──────────────────────────────────────────────
_whisper_model = None


def _get_whisper_model():
    """懒加载 faster-whisper small 模型（CPU int8）。"""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel(str(MODEL_DIR), device="cpu", compute_type="int8")
        logger.info("[Aimeeting] faster-whisper small 模型加载完成")
    return _whisper_model


# ── 会议纪要生成提示词（新工作流：一句话总结 + 结构化纪要）──────────
MINUTES_SYSTEM_PROMPT = """你是专业的会议记录助手，负责把会议语音转写文本整理成高质量的结构化会议纪要。

请严格按照以下 JSON 结构输出（不要输出任何额外文字、解释或 Markdown 代码块标记，只输出纯 JSON）：

{
  "summary": "用一句话（不超过 60 字）总结本次会议的核心结论",
  "minutes": "结构化会议纪要，使用 Markdown 格式，必须包含以下四个板块：\n## 结论\n## 讨论要点\n## 决议\n## 遗留问题"
}

注意事项：
- 总结需一句话说清会议最重要结论，简洁有力。
- 会议纪要必须忠实反映转写文本内容，不编造会议中未出现的信息。
- 「结论」概括会议最终达成的一致意见；「讨论要点」分条列出过程中的关键讨论；「决议」列出明确决定的事项；「遗留问题」列出未解决/待跟进事项。
- 若转写文本为空或过短，如实说明信息不足。
"""


# ── 会议自动命名 ─────────────────────────────────────────────────────────

def auto_title(now: datetime | None = None) -> str:
    """按时间自动生成会议名（本地时区）。"""
    t = now or datetime.now()
    return f"会议-{t.strftime('%Y.%m.%d %H:%M')}"


# ── 转写服务 ───────────────────────────────────────────────────────────

def _create_record_sync_data(
    meeting_id: int, offset_sec: int, audio_path: str, duration: int, device_id: str,
) -> AimeetingMinuteRecord:
    """构造转写记录 ORM 对象（未入库）。"""
    return AimeetingMinuteRecord(
        meeting_id=meeting_id,
        offset_sec=offset_sec,
        audio_path=audio_path,
        audio_duration=duration,
        transcript="",
        transcript_status=TranscriptStatus.PROCESSING,
        device_id=device_id,
        creator_id=0,
        creator_name="用户端",
    )


async def transcribe_audio_file(
    db: AsyncSession, meeting: AimeetingMeeting, audio_path: str, device_id: str = "",
    duration: int = 0, offset_sec: int = 0,
) -> str:
    """转写一段录音文件，返回转写文本（同步等待完成，供旧链路/测试使用）。

    使用 faster-whisper small 模型（CPU int8），离线本地推理。
    记录句级时间戳（会议内绝对时间 = 段偏移 + 段内时间）。
    转写结果写入 aimeeting_records 记录。
    """
    record_obj = _create_record_sync_data(meeting.id, offset_sec, audio_path, duration, device_id)
    db.add(record_obj)
    await db.commit()
    await db.refresh(record_obj)

    try:
        # CPU 密集任务放线程池，避免阻塞事件循环（分段上传时尤为关键）
        segments_data = await asyncio.to_thread(_transcribe_sync, audio_path, offset_sec)
        transcript = "\n".join(s["text"] for s in segments_data if s["text"].strip())
        record_obj.transcript = transcript
        record_obj.segments_json = json.dumps(segments_data, ensure_ascii=False)
        record_obj.transcript_status = TranscriptStatus.SUCCESS
        record_obj.error = ""
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[Aimeeting] 转写失败: {exc}")
        record_obj.transcript_status = TranscriptStatus.FAILED
        record_obj.error = f"转写失败：{exc}"

    await db.commit()
    await db.refresh(record_obj)

    # 转写完成后同步合并到会议 transcript_json（前端实时对话流立即可见）
    await merge_transcript_to_meeting(db, meeting)
    return record_obj.transcript


async def transcribe_record_async(record_id: int) -> None:
    """后台转写一条已登记的记录（独立数据库会话，不阻塞接口）。

    完成后把最新句级片段合并进会议 transcript_json，
    供前端实时对话流轮询展示。
    """
    from src.db import get_db_context

    async with get_db_context() as db:
        record_obj = await db.get(AimeetingMinuteRecord, record_id)
        if not record_obj or record_obj.transcript_status != TranscriptStatus.PROCESSING:
            return
        meeting = await db.get(AimeetingMeeting, record_obj.meeting_id)
        if not meeting or meeting.is_deleted:
            record_obj.transcript_status = TranscriptStatus.FAILED
            record_obj.error = "会议不存在或已删除"
            await db.commit()
            return

        try:
            segments_data = await asyncio.to_thread(
                _transcribe_sync, record_obj.audio_path, record_obj.offset_sec
            )
            record_obj.transcript = "\n".join(s["text"] for s in segments_data if s["text"].strip())
            record_obj.segments_json = json.dumps(segments_data, ensure_ascii=False)
            record_obj.transcript_status = TranscriptStatus.SUCCESS
            record_obj.error = ""
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Aimeeting] 后台转写失败 record={record_id}: {exc}")
            record_obj.transcript_status = TranscriptStatus.FAILED
            record_obj.error = f"转写失败：{exc}"
        await db.commit()

        # 合并到会议句级转写（实时对话流数据源）
        try:
            await merge_transcript_to_meeting(db, meeting)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Aimeeting] 合并转写失败 meeting={meeting.id}: {exc}")


async def wait_for_records_done(db: AsyncSession, meeting: AimeetingMeeting, timeout: float = 60.0) -> bool:
    """等待会议全部转写记录完成（结束会议前调用，防止漏掉最后几段）。

    返回 True 表示全部完成（或无记录）；False 表示超时仍有 processing。
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        processing = (
            await db.execute(
                select(AimeetingMinuteRecord.id).where(
                    AimeetingMinuteRecord.meeting_id == meeting.id,
                    AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
                    AimeetingMinuteRecord.transcript_status == TranscriptStatus.PROCESSING,
                )
            )
        ).scalars().first()
        if not processing:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(1.0)


def _transcribe_sync(audio_path: str, offset_sec: int) -> list[dict]:
    """同步转写（线程池内执行），返回句级片段 [{start, end, text}]（会议内绝对时间）。"""
    model = _get_whisper_model()
    segments, info = model.transcribe(
        audio_path,
        language="zh",
        beam_size=1,
        vad_filter=True,
    )
    result = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        result.append({
            "start": round(offset_sec + seg.start, 2),
            "end": round(offset_sec + seg.end, 2),
            "text": text,
        })
    return result


async def merge_transcript_to_meeting(db: AsyncSession, meeting: AimeetingMeeting) -> str:
    """合并会议全部有效转写片段到会议完整转写文本与句级 JSON。

    transcript_json 结构：[{start, end, text, speaker}]（speaker 由说话人分离回填，
    分离前为 0=未知）。
    """
    result = await db.execute(
        select(AimeetingMinuteRecord)
        .where(
            AimeetingMinuteRecord.meeting_id == meeting.id,
            AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
            AimeetingMinuteRecord.transcript_status == TranscriptStatus.SUCCESS,
        )
        .order_by(AimeetingMinuteRecord.id.asc())
    )
    records = result.scalars().all()

    all_segments: list[dict] = []
    for r in records:
        if not r.segments_json:
            # 兼容无句级数据的旧记录：整段文本作为一个片段
            if r.transcript.strip():
                all_segments.append({
                    "start": float(r.offset_sec or 0),
                    "end": float((r.offset_sec or 0) + (r.audio_duration or 0)),
                    "text": r.transcript.strip(),
                    "speaker": 0,
                })
            continue
        try:
            for seg in json.loads(r.segments_json):
                seg.setdefault("speaker", 0)
                all_segments.append(seg)
        except json.JSONDecodeError:
            logger.warning(f"[Aimeeting] record {r.id} segments_json 解析失败，跳过")

    all_segments.sort(key=lambda s: s["start"])
    meeting.transcript_text = "\n".join(s["text"] for s in all_segments if s["text"].strip())
    meeting.transcript_json = json.dumps(all_segments, ensure_ascii=False)
    meeting.transcript_status = (
        TranscriptStatus.SUCCESS if meeting.transcript_text.strip() else meeting.transcript_status
    )
    await db.commit()
    await db.refresh(meeting)
    return meeting.transcript_text


# ── 说话人分离 ─────────────────────────────────────────────────────────

async def run_diarization(db: AsyncSession, meeting: AimeetingMeeting) -> bool:
    """对会议音频跑说话人分离，输出说话人区间并对齐到句级转写。

    模型：sherpa-onnx（pyannote 分段 + Wespeaker 声纹，ONNX 离线）。
    流程：
    1. 拼接会议全部录音片段为完整音频（按 offset 排序，不足处补静音）。
    2. sherpa-onnx 跑 diarization，输出 [{start, end, speaker}]。
    3. 与 transcript_json 句级片段按时间重叠对齐，回填 speaker 编号。
    4. 生成/更新 aimeeting_speakers 表（说话人 1/2/3 + 说话时长）。

    返回是否成功。
    """
    if not DIARIZATION_DIR.exists():
        meeting.diarization_status = "failed"
        logger.warning(f"[Aimeeting] 说话人分离模型未下载：{DIARIZATION_DIR}，跳过")
        await db.commit()
        return False

    meeting.diarization_status = "pending"
    await db.commit()
    await db.refresh(meeting)

    try:
        # 1. 取全部录音片段
        result = await db.execute(
            select(AimeetingMinuteRecord)
            .where(
                AimeetingMinuteRecord.meeting_id == meeting.id,
                AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
            )
            .order_by(AimeetingMinuteRecord.offset_sec.asc())
        )
        records = result.scalars().all()
        if not records:
            meeting.diarization_status = "none"
            await db.commit()
            return False

        # 2. 拼接音频（线程池内执行，可能较慢）
        merged_audio = await asyncio.to_thread(_merge_audio_files, meeting, records)

        # 3. 说话人分离（线程池）
        diar_segments = await asyncio.to_thread(_diarize_sync, str(merged_audio))
        if not diar_segments:
            meeting.diarization_status = "failed"
            await db.commit()
            return False

        # 4. 对齐到句级转写
        if meeting.transcript_json:
            segments = json.loads(meeting.transcript_json)
            for seg in segments:
                seg["speaker"] = _match_speaker(seg, diar_segments)
            meeting.transcript_json = json.dumps(segments, ensure_ascii=False)
            # 同步纯文本格式：说话人 N: 文本
            meeting.transcript_text = "\n".join(
                f"说话人{s['speaker']}: {s['text']}" if s.get("speaker") else s["text"]
                for s in segments
            )

        # 5. 写入/更新 speakers 表
        speak_sec: dict[int, int] = {}
        for d in diar_segments:
            speak_sec[d["speaker"]] = speak_sec.get(d["speaker"], 0) + int(d["end"] - d["start"])
        existing = (
            await db.execute(select(AimeetingSpeaker).where(AimeetingSpeaker.meeting_id == meeting.id))
        ).scalars().all()
        existing_map = {s.speaker_no: s for s in existing}
        for no, sec in speak_sec.items():
            if no in existing_map:
                existing_map[no].total_speak_sec = sec
            else:
                db.add(AimeetingSpeaker(
                    meeting_id=meeting.id,
                    speaker_no=no,
                    display_name=f"说话人{no}",
                    total_speak_sec=sec,
                ))

        meeting.diarization_status = "success"
        await db.commit()
        await db.refresh(meeting)
        logger.info(f"[Aimeeting] 会议 {meeting.id} 说话人分离完成：{len(speak_sec)} 人")
        return True

    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[Aimeeting] 说话人分离失败: {exc}")
        meeting.diarization_status = "failed"
        await db.commit()
        return False


def _merge_audio_files(meeting: AimeetingMeeting, records) -> Path:
    """拼接会议录音片段为单个 16k 单声道 wav（按 offset 时间轴放置，间隙补静音）。

    关键：句级转写的时间戳是会议绝对时间（offset + 片段内时间），说话人分离
    必须与之一致，因此合并产物必须保持绝对时间轴——片段按 offset 落到对应
    位置，空隙填静音，而不是简单顺序拼接（否则时间轴漂移导致无法对齐）。
    """
    output_path = AUDIO_STORE_DIR / f"m{meeting.id}_merged.wav"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 计算总时长（最后一片的偏移+时长）
    total_sec = max((r.offset_sec or 0) + (r.audio_duration or 0) for r in records)
    inputs = []
    for r in records:
        if r.audio_path and Path(r.audio_path).exists():
            inputs.append((r.audio_path, r.offset_sec or 0))

    import numpy as np
    import wave

    def _read_16k_mono(p: Path) -> np.ndarray:
        """读取音频为 16k 单声道 float32 数组。"""
        import ffmpeg
        import io
        out, _ = (
            ffmpeg
            .input(str(p))
            .output("pipe:1", format="wav", ac=1, ar=16000)
            .run(capture_stdout=True, quiet=True)
        )
        with wave.open(io.BytesIO(out), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        return arr

    if len(inputs) == 1 and inputs[0][1] == 0:
        # 单文件且 offset=0：直接转 wav（保持原逻辑）
        import ffmpeg
        (
            ffmpeg
            .input(inputs[0][0])
            .output(str(output_path), ac=1, ar=16000)
            .overwrite_output()
            .run(quiet=True)
        )
    else:
        # 总采样数（16k 单声道）
        total_samples = max(int(total_sec * 16000), 1)
        merged = np.zeros(total_samples, dtype=np.float32)
        for path, offset_sec in inputs:
            arr = _read_16k_mono(path)
            start = int(offset_sec * 16000)
            end = min(start + len(arr), total_samples)
            if end > start:
                merged[start:end] = arr[: end - start]

        # 写 16k 单声道 wav
        pcm = (np.clip(merged, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(str(output_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm.tobytes())

    meeting.audio_file = str(output_path)
    meeting.audio_duration = int(total_sec)
    return output_path


def _diarize_sync(audio_path: str) -> list[dict]:
    """同步执行 sherpa-onnx 说话人分离（线程池内）。

    官方 API：``sd.process(float32_audio)`` 直接传波形数组（16k 单声道），
    返回 ``sort_by_start_time()`` 的说话人区间列表。
    """
    import sherpa_onnx

    # 模型目录约定：segmentation.onnx + embedding.onnx（已预下载放置）
    seg_model = DIARIZATION_DIR / "segmentation.onnx"
    emb_model = DIARIZATION_DIR / "embedding.onnx"

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(seg_model),
            ),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(emb_model),
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=-1,  # 自动估计人数
            threshold=0.5,
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)

    # 读取 16k 单声道 wav，转 float32
    import wave

    import numpy as np

    with wave.open(audio_path, "rb") as wf:
        assert wf.getframerate() == 16000, "要求 16kHz wav"
        assert wf.getnchannels() == 1, "要求单声道"
        frames = wf.readframes(wf.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

    result = sd.process(samples).sort_by_start_time()
    segments = []
    for seg in result:
        segments.append({
            "start": float(seg.start),
            "end": float(seg.end),
            "speaker": int(seg.speaker) + 1,  # 1 起
        })
    return segments


def _match_speaker(seg: dict, diar_segments: list[dict]) -> int:
    """句级片段与说话人区间按时间重叠最大者对齐。"""
    best_speaker = 0
    best_overlap = 0.0
    for d in diar_segments:
        overlap = min(seg["end"], d["end"]) - max(seg["start"], d["start"])
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = d["speaker"]
    return best_speaker


# ── 会议纪要生成 ─────────────────────────────────────────────────────────

async def _build_source_material(meeting: AimeetingMeeting) -> str:
    """将会议信息与完整转写文本拼接为 AI 输入的文本素材。"""
    lines: list[str] = []
    lines.append(f"会议标题：{meeting.title}")
    if meeting.participants:
        lines.append(f"参会人：{meeting.participants}")
    if meeting.start_time:
        lines.append(f"会议时间：{meeting.start_time.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("【会议语音转写全文】")
    if meeting.transcript_text.strip():
        lines.append(meeting.transcript_text)
    else:
        lines.append("（暂无转写文本）")
    return "\n".join(lines)


async def generate_minutes(
    db: AsyncSession, meeting: AimeetingMeeting, user_id: int = 0
) -> AimeetingMinutes:
    """为指定会议生成会议总结（一句话）与结构化会议纪要。

    流程：
    1. 校验会议存在。
    2. 拼接转写文本为素材。
    3. 获取启用中的 AI 供应商，调用模型生成。
    4. 解析 JSON 结果，写入 aimeeting_minutes 表。
    5. 若 AI 未配置或调用失败，写入失败状态供前端提示。
    """
    source_material = await _build_source_material(meeting)

    result = await db.execute(
        select(AimeetingMinutes).where(AimeetingMinutes.meeting_id == meeting.id)
    )
    minutes_row = result.scalars().first()
    if not minutes_row:
        minutes_row = AimeetingMinutes(meeting_id=meeting.id)
        db.add(minutes_row)

    minutes_row.status = "pending"
    minutes_row.source_material = source_material
    minutes_row.error = ""
    minutes_row.generated_by = user_id or 0
    await db.commit()
    await db.refresh(minutes_row)

    provider = await _get_enabled_provider_or_none(db)
    if provider is None:
        minutes_row.status = "failed"
        minutes_row.error = "未配置可用的 AI 模型供应商，请先在「模型密钥管理」中添加"
        await db.commit()
        await db.refresh(minutes_row)
        return minutes_row

    try:
        messages = [
            {
                "role": "user",
                "content": (
                    f"{MINUTES_SYSTEM_PROMPT}\n\n"
                    "以下是本次会议的语音转写文本：\n\n"
                    f"{source_material}\n\n"
                    "请根据以上转写文本生成会议总结和会议纪要，严格按要求的 JSON 结构输出。"
                ),
            }
        ]
        result_dict = await chat_non_stream(
            messages=messages,
            provider=provider,
            max_tokens=4000,
            temperature=0.3,
            enable_tools=False,
        )
        content = result_dict.get("content", "").strip()
        parsed = _parse_ai_content(content)

        minutes_row.summary = parsed.get("summary", "")
        minutes_row.minutes = parsed.get("minutes", content)
        minutes_row.status = "success"
        minutes_row.provider_name = provider.name
        minutes_row.error = ""
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[Aimeeting] 生成会议纪要失败: {exc}")
        minutes_row.status = "failed"
        minutes_row.error = f"生成失败：{exc}"
        minutes_row.provider_name = provider.name if provider else ""

    await db.commit()
    await db.refresh(minutes_row)
    return minutes_row


async def _get_enabled_provider_or_none(db: AsyncSession):
    """获取第一个启用的 AI 供应商，无则返回 None。"""
    try:
        return await crud_ai_provider.get_first_enabled(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[aimeeting] 获取 AI 供应商失败: {exc}")
        return None


def _parse_ai_content(content: str) -> dict:
    """解析 AI 返回的 JSON 内容，容错处理 markdown 包裹。"""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        return {"summary": "", "minutes": text}


def generate_meeting_code() -> str:
    """生成会议编号（8 位大写字母+数字，如 A1B2C3D4）。"""
    import random
    import string

    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=8))


# ── 会后处理链路（结束后：等待转写完成 → 说话人分离 → AI 纪要）──────────

async def finalize_meeting(meeting_id: int) -> None:
    """会议结束后的完整处理链路（按序执行，失败不中断后续步骤）。

    在独立后台任务中运行，使用自己的数据库会话。
    """
    from src.db import get_db_context

    async with get_db_context() as db:
        meeting = await db.get(AimeetingMeeting, meeting_id)
        if not meeting or meeting.is_deleted:
            logger.warning(f"[Aimeeting] finalize 会议 {meeting_id} 不存在，跳过")
            return

        # 0. 等待全部转写记录完成（最多 90 秒，防止结束时最后几段漏掉）
        done = await wait_for_records_done(db, meeting, timeout=90.0)
        if not done:
            logger.warning(f"[Aimeeting] 会议 {meeting_id} 转写等待超时，继续后续处理")

        # 1. 合并句级转写
        try:
            await merge_transcript_to_meeting(db, meeting)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Aimeeting] 合并转写失败: {exc}")

        # 2. 说话人分离（模型存在才跑）
        if DIARIZATION_DIR.exists() and meeting.transcript_text.strip():
            try:
                await run_diarization(db, meeting)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[Aimeeting] 说话人分离失败: {exc}")
        else:
            meeting.diarization_status = "none"
            await db.commit()
            await db.refresh(meeting)

        # 3. AI 纪要（说话人标注后的文本作为素材，纪要质量更高）
        try:
            await generate_minutes(db, meeting, user_id=0)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Aimeeting] 生成纪要失败: {exc}")
