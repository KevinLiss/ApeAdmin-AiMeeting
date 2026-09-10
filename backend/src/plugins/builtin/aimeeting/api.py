"""AI 会议助手插件——API 路由。

权限说明：
- 管理端接口：沿用 ``aimeeting:`` 权限前缀（需登录）。
- 用户端接口：通过「会议编号 + 设备标识」无感认证，不依赖登录。

核心工作流：
开始会议进行录音（15 秒切片上传）→ 后台转写（faster-whisper，句级时间戳）
→ 实时对话流展示 → 结束会议 → 说话人分离（会后批处理）→ LLM 总结 + 结构化纪要
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import get_current_user
from src.core.deps import require_permission as _require_perm
from src.core.exceptions import success_response
from src.db import get_db
from src.models import User
from src.plugins.builtin.aimeeting.models import (
    AimeetingMeeting,
    AimeetingMinuteRecord,
    AimeetingMinutes,
    AimeetingSpeaker,
    MeetingStatus,
    TranscriptStatus,
)
from src.plugins.builtin.aimeeting.schemas import (
    DeviceBind,
    MeetingCreate,
    MeetingLookup,
    MeetingOut,
    MeetingRename,
    MeetingStatusUpdate,
    MeetingUpdate,
    MinutesOut,
    RecordOut,
    SpeakerOut,
    SpeakerUpdate,
)
from src.plugins.builtin.aimeeting.services import (
    AUDIO_STORE_DIR,
    auto_title,
    finalize_meeting,
    generate_meeting_code,
    generate_minutes,
    merge_transcript_to_meeting,
    speaker_code,
    transcribe_record_async,
    wait_for_records_done,
)

router = APIRouter(prefix="/aimeeting", tags=["AI 会议助手"])


def _now() -> datetime:
    """当前 UTC 时间。"""
    return datetime.now(timezone.utc)


async def _get_meeting_or_404(db: AsyncSession, meeting_id: int) -> AimeetingMeeting:
    """获取会议，不存在或已删除则抛 404。"""
    meeting = await db.get(AimeetingMeeting, meeting_id)
    if not meeting or meeting.is_deleted:
        raise HTTPException(status_code=404, detail="会议不存在")
    return meeting


async def _get_meeting_by_code_or_404(db: AsyncSession, meeting_code: str) -> AimeetingMeeting:
    """通过会议编号获取会议。"""
    result = await db.execute(
        select(AimeetingMeeting).where(
            AimeetingMeeting.meeting_code == meeting_code,
            AimeetingMeeting.is_deleted == False,  # noqa: E712
        )
    )
    meeting = result.scalars().first()
    if not meeting:
        raise HTTPException(status_code=404, detail="会议编号不存在")
    return meeting


async def _check_device(meeting: AimeetingMeeting, device_id: str):
    """轻量设备校验（不提交，改由调用方统一 commit）。"""
    if not meeting.device_id:
        meeting.device_id = device_id
    elif meeting.device_id != device_id:
        raise HTTPException(status_code=403, detail="该会议已绑定其他设备")


async def _check_meeting(db: AsyncSession, meeting: AimeetingMeeting, device_id: str):
    """校验设备访问权限并绑定。

    注意：首次绑定会触发 ``db.commit()``，此时 ``updated_at`` 等由数据库
    ``onupdate`` 生成的字段会过期，必须在 commit 后 ``refresh`` 整个对象，
    否则后续 ``MeetingOut.model_validate`` 序列化时访问过期属性会触发
    MissingGreenlet。
    """
    if not meeting.device_id:
        meeting.device_id = device_id
        await db.commit()
        await db.refresh(meeting)
    elif meeting.device_id != device_id:
        raise HTTPException(status_code=403, detail="该会议已绑定其他设备")


def _sanitize_client_meeting(data: dict) -> dict:
    """用户端只暴露必要字段。"""
    data.pop("transcript_text", None)
    data.pop("transcript_json", None)
    data.pop("device_id", None)
    data.pop("creator_id", None)
    data.pop("creator_name", None)
    data.pop("audio_file", None)
    return data


# ─────────────────────────────────────────────────────────────────────────
# 用户端接口（无感登录：会议编号 + 设备标识）
# ─────────────────────────────────────────────────────────────────────────

@router.post("/client/meetings/lookup")
async def client_lookup_meeting(
    body: MeetingLookup,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """用户端：输入会议编号查询会议（无感绑定设备）。"""
    meeting = await _get_meeting_by_code_or_404(db, body.meeting_code.strip().upper())
    await _check_meeting(db, meeting, body.device_id)
    data = MeetingOut.model_validate(meeting).model_dump(mode="json")
    return success_response(data=_sanitize_client_meeting(data))


@router.post("/client/meetings")
async def client_create_meeting(
    body: MeetingCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """用户端：创建会议（名称可选，留空按时间自动命名）。"""
    # 生成唯一会议编号
    code = generate_meeting_code()
    while True:
        exists = (
            await db.execute(
                select(AimeetingMeeting.id).where(AimeetingMeeting.meeting_code == code)
            )
        ).scalars().first()
        if not exists:
            break
        code = generate_meeting_code()

    title = body.title.strip() or auto_title()
    meeting = AimeetingMeeting(
        title=title,
        meeting_code=code,
        participants=body.participants,
        start_time=body.start_time,
        status=MeetingStatus.SCHEDULED,
        creator_id=0,
        creator_name="用户端",
    )
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)
    return success_response(
        data=MeetingOut.model_validate(meeting).model_dump(mode="json"),
        msg="会议创建成功，会议编号：" + code,
    )


@router.patch("/client/meetings/{meeting_id}")
async def client_rename_meeting(
    meeting_id: int,
    body: MeetingRename,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """用户端：修改会议名称（全程可改）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_device(meeting, body.device_id)
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(status_code=422, detail="会议名称不能为空")
    meeting.title = new_title
    await db.commit()
    await db.refresh(meeting)
    return success_response(
        data=MeetingOut.model_validate(meeting).model_dump(mode="json"),
        msg="会议名称已更新",
    )


@router.post("/client/meetings/{meeting_id}/audio")
async def client_upload_audio(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
    device_id: str = Form(...),
    duration: int = Form(default=0),
    offset_sec: int = Form(default=0, ge=0),
):
    """用户端：上传一段录音切片，登记后立即返回，转写在后台执行。

    15 秒实时切片方案：前端每 15 秒上传一段，携带会议内偏移秒数。
    接口立即返回（转写后台异步），前端轮询 /client/meetings/{id} 获取
    最新 transcript_json 渲染实时对话流。
    """
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_device(meeting, device_id)

    # 会议状态流转：进行中
    if meeting.status == MeetingStatus.SCHEDULED:
        meeting.status = MeetingStatus.IN_PROGRESS
        meeting.actual_start = meeting.actual_start or _now()

    # 校验文件类型与大小（≤50MB）
    if file.size and file.size > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="录音文件过大（上限 50MB）")

    # 保存录音文件
    AUDIO_STORE_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "audio.webm").suffix.lower() or ".webm"
    if ext not in {".webm", ".ogg", ".wav", ".m4a", ".mp3", ".mp4", ".aac"}:
        ext = ".webm"
    saved_name = f"m{meeting_id}_{uuid.uuid4().hex[:12]}{ext}"
    saved_path = AUDIO_STORE_DIR / saved_name
    content = await file.read()
    with open(saved_path, "wb") as f:
        f.write(content)

    # 登记转写记录（processing 状态），后台异步转写
    record = AimeetingMinuteRecord(
        meeting_id=meeting.id,
        offset_sec=offset_sec,
        audio_path=str(saved_path),
        audio_duration=duration,
        transcript="",
        transcript_status=TranscriptStatus.PROCESSING,
        device_id=device_id,
        creator_id=0,
        creator_name="用户端",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    # 后台转写（不阻塞本接口，前端轮询看结果）
    asyncio.create_task(transcribe_record_async(record.id))

    return success_response(
        data={"record_id": record.id, "transcript_status": record.transcript_status},
        msg="录音已接收，正在转写",
    )


@router.post("/client/meetings/{meeting_id}/start")
async def client_start_meeting(
    meeting_id: int,
    body: DeviceBind,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """用户端：正式开始会议（开始计时，状态流转 scheduled → in_progress）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_device(meeting, body.device_id)

    if meeting.status == MeetingStatus.ENDED:
        raise HTTPException(status_code=409, detail="会议已结束")
    if meeting.status != MeetingStatus.IN_PROGRESS:
        meeting.status = MeetingStatus.IN_PROGRESS
        meeting.actual_start = meeting.actual_start or _now()
        await db.commit()
        await db.refresh(meeting)

    return success_response(
        data={"status": meeting.status, "actual_start": meeting.actual_start.isoformat() if meeting.actual_start else None},
        msg="会议已开始",
    )


@router.post("/client/meetings/{meeting_id}/finish")
async def client_finish_meeting(
    meeting_id: int,
    body: DeviceBind,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """用户端：结束会议。

    文字记录模式：直接结束存档（后台可查看会议记录）。
    若有录音：等待在途转写完成 → 合并 → 触发会后链路（说话人分离 + AI 纪要）。
    """
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_device(meeting, body.device_id)

    # 是否存在录音记录（文字模式没有录音则跳过语音链路）
    has_records = (
        await db.execute(
            select(AimeetingMinuteRecord.id)
            .where(
                AimeetingMinuteRecord.meeting_id == meeting.id,
                AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
            )
            .limit(1)
        )
    ).scalars().first()

    if has_records:
        # 等待在途转写完成（最多 30 秒）
        done = await wait_for_records_done(db, meeting, timeout=30.0)
        # 合并转写（兼容部分场景直接落库）
        await merge_transcript_to_meeting(db, meeting)
        # 会后链路放后台任务（说话人分离 + AI 纪要可能耗时较长）
        asyncio.ensure_future(finalize_meeting(meeting.id))
    else:
        done = True

    meeting.status = MeetingStatus.ENDED
    meeting.actual_end = meeting.actual_end or _now()
    meeting.audio_duration = meeting.audio_duration or 0
    await db.commit()
    await db.refresh(meeting)

    return success_response(
        data={"status": meeting.status, "transcripts_done": done},
        msg="会议已结束" + ("" if has_records else "，会议记录已存档"),
    )


@router.get("/client/meetings/{meeting_id}")
async def client_get_meeting(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    device_id: str = Query(..., min_length=8),
):
    """用户端：查询会议详情（含实时对话流、转写进度、纪要状态、说话人）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_meeting(db, meeting, device_id)

    minutes_row = (
        await db.execute(
            select(AimeetingMinutes).where(AimeetingMinutes.meeting_id == meeting.id)
        )
    ).scalars().first()

    speakers = (
        await db.execute(
            select(AimeetingSpeaker).where(
                AimeetingSpeaker.meeting_id == meeting.id,
                AimeetingSpeaker.is_deleted == False,  # noqa: E712
            )
            .order_by(AimeetingSpeaker.speaker_no.asc())
        )
    ).scalars().all()

    # 在途/失败记录数（前端显示"转写中 N 段"）
    processing_count = (
        await db.execute(
            select(func.count(AimeetingMinuteRecord.id)).where(
                AimeetingMinuteRecord.meeting_id == meeting.id,
                AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
                AimeetingMinuteRecord.transcript_status == TranscriptStatus.PROCESSING,
            )
        )
    ).scalar() or 0
    failed_count = (
        await db.execute(
            select(func.count(AimeetingMinuteRecord.id)).where(
                AimeetingMinuteRecord.meeting_id == meeting.id,
                AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
                AimeetingMinuteRecord.transcript_status == TranscriptStatus.FAILED,
            )
        )
    ).scalar() or 0

    data = MeetingOut.model_validate(meeting).model_dump(mode="json")
    # 用户端保留 transcript_json（实时对话流数据源）但去掉完整纯文本冗余
    data["transcript_json"] = meeting.transcript_json or "[]"
    data["minutes"] = (
        MinutesOut.model_validate(minutes_row).model_dump(mode="json")
        if minutes_row
        else None
    )
    data["speakers"] = [
        SpeakerOut.model_validate(s).model_dump(mode="json") for s in speakers
    ]
    data["processing_count"] = processing_count
    data["failed_count"] = failed_count
    _sanitize_client_meeting(data)
    # transcript_json 需要保留（sanitizer 只删 transcript_text）
    data.pop("transcript_text", None)
    return success_response(data=data)


@router.get("/client/meetings/{meeting_id}/transcript")
async def client_get_transcript(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    device_id: str = Query(..., min_length=8),
):
    """用户端：轮询接口——轻量返回最新句级转写（实时对话流增量渲染）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_device(meeting, device_id)

    processing_count = (
        await db.execute(
            select(func.count(AimeetingMinuteRecord.id)).where(
                AimeetingMinuteRecord.meeting_id == meeting.id,
                AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
                AimeetingMinuteRecord.transcript_status == TranscriptStatus.PROCESSING,
            )
        )
    ).scalar() or 0

    try:
        segments = json.loads(meeting.transcript_json or "[]")
    except json.JSONDecodeError:
        segments = []

    # 说话人显示名映射（speaker_no -> display_name）
    speakers = (
        await db.execute(
            select(AimeetingSpeaker).where(
                AimeetingSpeaker.meeting_id == meeting.id,
                AimeetingSpeaker.is_deleted == False,  # noqa: E712
            )
        )
    ).scalars().all()
    speaker_names = {s.speaker_no: s.display_name for s in speakers}

    for seg in segments:
        no = seg.get("speaker", 0)
        seg["speaker_name"] = speaker_names.get(no, f"发言者{speaker_code(no)}" if no else "")

    return success_response(data={
        "segments": segments,
        "processing_count": processing_count,
        "transcript_status": meeting.transcript_status,
        "diarization_status": meeting.diarization_status,
        "updated_at": meeting.updated_at.isoformat() if meeting.updated_at else None,
    })


@router.patch("/client/meetings/{meeting_id}/speakers/{speaker_id}")
async def client_update_speaker(
    meeting_id: int,
    speaker_id: int,
    body: SpeakerUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    device_id: str = Query(..., min_length=8),
):
    """用户端：修改说话人显示名称（如「说话人1」→「张三」）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_device(meeting, device_id)

    speaker = await db.get(AimeetingSpeaker, speaker_id)
    if not speaker or speaker.meeting_id != meeting.id or speaker.is_deleted:
        raise HTTPException(status_code=404, detail="说话人不存在")

    speaker.display_name = body.display_name.strip()
    if not speaker.display_name:
        raise HTTPException(status_code=422, detail="显示名称不能为空")
    await db.commit()
    await db.refresh(speaker)
    return success_response(
        data=SpeakerOut.model_validate(speaker).model_dump(mode="json"),
        msg="说话人名称已更新",
    )


# ─────────────────────────────────────────────────────────────────────────
# 管理端接口（需登录 + aimeeting: 权限）
# ─────────────────────────────────────────────────────────────────────────

@router.get("/meetings")
async def list_meetings(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:meeting:list"))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str = Query(default="", description="按状态筛选"),
    keyword: str = Query(default="", description="标题/编号搜索关键词"),
):
    """管理端：分页查询会议列表（历史会议管理）。"""
    stmt = select(AimeetingMeeting).where(AimeetingMeeting.is_deleted == False)  # noqa: E712
    if status:
        stmt = stmt.where(AimeetingMeeting.status == status)
    if keyword:
        stmt = stmt.where(
            (AimeetingMeeting.title.ilike(f"%{keyword}%"))
            | (AimeetingMeeting.meeting_code.ilike(f"%{keyword}%"))
        )

    count_stmt = stmt
    total = len((await db.execute(count_stmt)).scalars().all())

    stmt = (
        stmt.order_by(AimeetingMeeting.start_time.desc().nullslast(), AimeetingMeeting.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()
    if not items:
        return success_response(data={
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [],
        })

    meeting_ids = [m.id for m in items]
    record_counts = dict(
        (
            await db.execute(
                select(AimeetingMinuteRecord.meeting_id, func.count(AimeetingMinuteRecord.id))
                .where(
                    AimeetingMinuteRecord.meeting_id.in_(meeting_ids),
                    AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
                )
                .group_by(AimeetingMinuteRecord.meeting_id)
            )
        ).all()
    )
    minutes_rows = (
        await db.execute(
            select(AimeetingMinutes.meeting_id, AimeetingMinutes.status).where(
                AimeetingMinutes.meeting_id.in_(meeting_ids)
            )
        )
    ).all()
    minutes_status_map = {mid: status for mid, status in minutes_rows}

    result_items = []
    for m in items:
        data = MeetingOut.model_validate(m).model_dump(mode="json")
        data["record_count"] = record_counts.get(m.id, 0)
        data["minutes_status"] = minutes_status_map.get(m.id, "none")
        result_items.append(data)

    return success_response(data={
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": result_items,
    })


@router.get("/meetings/{meeting_id}")
async def get_meeting(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:meeting:list"))],
):
    """管理端：查询单个会议详情（含记录条数与纪要状态）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    record_count = len(
        (
            await db.execute(
                select(AimeetingMinuteRecord).where(
                    AimeetingMinuteRecord.meeting_id == meeting.id,
                    AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
                )
            )
        ).scalars().all()
    )
    minutes_row = (
        await db.execute(
            select(AimeetingMinutes).where(AimeetingMinutes.meeting_id == meeting.id)
        )
    ).scalars().first()
    speakers = (
        await db.execute(
            select(AimeetingSpeaker).where(
                AimeetingSpeaker.meeting_id == meeting.id,
                AimeetingSpeaker.is_deleted == False,  # noqa: E712
            )
            .order_by(AimeetingSpeaker.speaker_no.asc())
        )
    ).scalars().all()

    data = MeetingOut.model_validate(meeting).model_dump(mode="json")
    data["record_count"] = record_count
    data["minutes_status"] = minutes_row.status if minutes_row else "none"
    data["minutes"] = (
        MinutesOut.model_validate(minutes_row).model_dump(mode="json")
        if minutes_row
        else None
    )
    data["speakers"] = [
        SpeakerOut.model_validate(s).model_dump(mode="json") for s in speakers
    ]
    return success_response(data=data)


@router.post("/meetings")
async def create_meeting(
    body: MeetingCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:meeting:create"))],
):
    """管理端：创建会议（名称可选，留空自动命名）。"""
    code = generate_meeting_code()
    while True:
        exists = (
            await db.execute(
                select(AimeetingMeeting.id).where(AimeetingMeeting.meeting_code == code)
            )
        ).scalars().first()
        if not exists:
            break
        code = generate_meeting_code()
    title = body.title.strip() or auto_title()
    meeting = AimeetingMeeting(
        title=title,
        meeting_code=code,
        participants=body.participants,
        start_time=body.start_time,
        status=MeetingStatus.SCHEDULED,
        creator_id=user.id,
        creator_name=user.nickname or user.username,
    )
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)
    return success_response(data=MeetingOut.model_validate(meeting).model_dump(mode="json"), msg=f"会议创建成功，编号 {code}")


@router.put("/meetings/{meeting_id}")
async def update_meeting(
    meeting_id: int,
    body: MeetingUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:meeting:edit"))],
):
    """管理端：更新会议基本信息（含改名）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(meeting, key, value)
    await db.commit()
    await db.refresh(meeting)
    return success_response(data=MeetingOut.model_validate(meeting).model_dump(mode="json"), msg="会议更新成功")


@router.put("/meetings/{meeting_id}/status")
async def update_meeting_status(
    meeting_id: int,
    body: MeetingStatusUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:meeting:edit"))],
):
    """管理端：流转会议状态。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    new_status = body.status
    valid_statuses = {
        MeetingStatus.SCHEDULED,
        MeetingStatus.IN_PROGRESS,
        MeetingStatus.ENDED,
        MeetingStatus.CANCELLED,
    }
    if new_status not in valid_statuses:
        raise HTTPException(status_code=422, detail="无效的会议状态")

    meeting.status = new_status
    if new_status == MeetingStatus.IN_PROGRESS and not meeting.actual_start:
        meeting.actual_start = _now()
    if new_status == MeetingStatus.ENDED and not meeting.actual_end:
        meeting.actual_end = _now()
    await db.commit()
    await db.refresh(meeting)
    return success_response(data=MeetingOut.model_validate(meeting).model_dump(mode="json"), msg="会议状态已更新")


@router.delete("/meetings/{meeting_id}")
async def delete_meeting(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:meeting:delete"))],
):
    """管理端：删除会议（软删除，同时删除其记录与纪要）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    meeting.is_deleted = True
    await db.execute(
        AimeetingMinuteRecord.__table__.update()
        .where(AimeetingMinuteRecord.meeting_id == meeting.id)
        .values(is_deleted=True)
    )
    await db.execute(
        AimeetingSpeaker.__table__.update()
        .where(AimeetingSpeaker.meeting_id == meeting.id)
        .values(is_deleted=True)
    )
    await db.commit()
    return success_response(msg="会议删除成功")


# ── 转写记录（管理端查看） ─────────────────────────────────────────────

@router.get("/meetings/{meeting_id}/records")
async def list_records(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:record:list"))],
):
    """管理端：查询会议的转写记录列表（多段录音的转写结果）。"""
    await _get_meeting_or_404(db, meeting_id)
    result = await db.execute(
        select(AimeetingMinuteRecord)
        .where(
            AimeetingMinuteRecord.meeting_id == meeting_id,
            AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
        )
        .order_by(AimeetingMinuteRecord.id.asc())
    )
    records = result.scalars().all()
    return success_response(data=[
        RecordOut.model_validate(r).model_dump(mode="json") for r in records
    ])


@router.post("/meetings/{meeting_id}/minutes/generate")
async def generate_meeting_minutes(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:minutes:generate"))],
):
    """管理端：触发 AI 生成会议总结与会议纪要。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    minutes_row = await generate_minutes(db, meeting, user_id=user.id)
    return success_response(
        data=MinutesOut.model_validate(minutes_row).model_dump(mode="json"),
        msg="生成成功" if minutes_row.status == "success" else "生成未完成",
    )


@router.get("/meetings/{meeting_id}/minutes")
async def get_meeting_minutes(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:minutes:list"))],
):
    """管理端：查询会议的 AI 纪要。"""
    await _get_meeting_or_404(db, meeting_id)
    minutes_row = (
        await db.execute(
            select(AimeetingMinutes).where(AimeetingMinutes.meeting_id == meeting_id)
        )
    ).scalars().first()
    if not minutes_row:
        return success_response(data=None)
    return success_response(data=MinutesOut.model_validate(minutes_row).model_dump(mode="json"))
