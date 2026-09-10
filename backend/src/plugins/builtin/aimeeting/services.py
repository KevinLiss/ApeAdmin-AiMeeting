"""AI 会议助手插件——业务服务层。

核心工作流：
开始会议进行录音 → 语音转写（faster-whisper）→ LLM 总结 1 句话 → 结构化会议纪要输出
（结论、讨论要点、决议、遗留问题）
"""
import json
import os
import uuid
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
    TranscriptStatus,
)

# ── 路径配置 ─────────────────────────────────────────────────────────────
# 录音文件存储目录（相对 backend 根目录，可被配置覆盖）
BACKEND_DIR = Path(__file__).resolve().parents[4]  # backend/
AUDIO_STORE_DIR = Path(os.environ.get("AIMEETING_AUDIO_DIR", BACKEND_DIR / "storage" / "aimeeting" / "audio"))
MODEL_DIR = Path(os.environ.get("AIMEETING_WHISPER_MODEL", BACKEND_DIR / "models" / "faster-whisper-small"))

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


# ── 转写服务 ───────────────────────────────────────────────────────────

async def transcribe_audio_file(
    db: AsyncSession, meeting: AimeetingMeeting, audio_path: str, device_id: str = "", duration: int = 0
) -> str:
    """转写一段录音文件，返回转写文本。

    使用 faster-whisper small 模型（CPU int8），离线本地推理。
    转写结果写入 aimeeting_records 记录，并累加到会议完整转写文本。
    """
    record_obj = AimeetingMinuteRecord(
        meeting_id=meeting.id,
        audio_path=audio_path,
        audio_duration=duration,
        transcript="",
        transcript_status=TranscriptStatus.PROCESSING,
        device_id=device_id,
        creator_id=0,
        creator_name="用户端",
    )
    db.add(record_obj)
    await db.commit()
    await db.refresh(record_obj)

    try:
        model = _get_whisper_model()
        segments, info = model.transcribe(
            audio_path,
            language="zh",
            beam_size=1,
            vad_filter=True,
        )
        parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        transcript = "\n".join(parts)

        record_obj.transcript = transcript
        record_obj.transcript_status = TranscriptStatus.SUCCESS if transcript else TranscriptStatus.SUCCESS
        record_obj.error = ""
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[Aimeeting] 转写失败: {exc}")
        record_obj.transcript_status = TranscriptStatus.FAILED
        record_obj.error = f"转写失败：{exc}"

    await db.commit()
    await db.refresh(record_obj)
    return record_obj.transcript


async def merge_transcript_to_meeting(db: AsyncSession, meeting: AimeetingMeeting) -> str:
    """合并会议全部有效转写片段到会议完整转写文本。"""
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
    meeting.transcript_text = "\n\n".join(r.transcript for r in records if r.transcript.strip())
    await db.commit()
    await db.refresh(meeting)
    return meeting.transcript_text


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