"""AI 会议助手插件——API 路由。

权限说明：
- 管理端接口：沿用 ``aimeeting:`` 权限前缀（需登录）。
- 用户端接口：通过「会议编号 + 设备标识」无感认证，不依赖登录。

核心工作流：
开始会议进行录音 → 上传录音 → 语音转写（faster-whisper）→ LLM 总结 1 句话 → 结构化会议纪要输出
"""
import os
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
    MeetingStatus,
    TranscriptStatus,
)
from src.plugins.builtin.aimeeting.schemas import (
    AudioUpload,
    DeviceBind,
    MeetingCreate,
    MeetingLookup,
    MeetingOut,
    MeetingStatusUpdate,
    MeetingUpdate,
    MinutesOut,
    RecordOut,
)
from src.plugins.builtin.aimeeting.services import (
    AUDIO_STORE_DIR,
    generate_meeting_code,
    generate_minutes,
    merge_transcript_to_meeting,
    transcribe_audio_file,
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
    """校验设备是否可访问该会议。

    首次访问自动绑定；之后仅允许同一设备访问，防止编号泄露被他人读取。
    """
    if not meeting.device_id:
        meeting.device_id = device_id
    elif meeting.device_id != device_id:
        raise HTTPException(status_code=403, detail="该会议已绑定其他设备")


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
    await db.refresh(meeting)
    data = MeetingOut.model_validate(meeting).model_dump(mode="json")
    # 用户端只暴露必要信息
    data.pop("transcript_text", None)
    data.pop("device_id", None)
    data.pop("creator_id", None)
    data.pop("creator_name", None)
    return success_response(data=data)


@router.post("/client/meetings")
async def client_create_meeting(
    body: MeetingCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """用户端：创建会议（只需名称、时间、参会人）。"""
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

    meeting = AimeetingMeeting(
        title=body.title.strip(),
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


@router.post("/client/meetings/{meeting_id}/audio")
async def client_upload_audio(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
    device_id: str = Form(...),
    duration: int = Form(default=0),
):
    """用户端：上传一段录音文件，触发本地语音转写。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_meeting(db, meeting, device_id)

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

    # 会议状态流转：进行中
    if meeting.status == MeetingStatus.SCHEDULED:
        meeting.status = MeetingStatus.IN_PROGRESS
        meeting.actual_start = meeting.actual_start or _now()

    # 触发转写（同步等待，small 模型 CPU 较快）
    transcript = await transcribe_audio_file(
        db, meeting, str(saved_path), device_id=device_id, duration=duration
    )
    # 合并到会议完整转写
    full_text = await merge_transcript_to_meeting(db, meeting)
    await db.commit()

    return success_response(
        data={"transcript": transcript, "full_transcript": full_text},
        msg="录音已上传并完成转写",
    )


@router.post("/client/meetings/{meeting_id}/finish")
async def client_finish_meeting(
    meeting_id: int,
    body: DeviceBind,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """用户端：结束会议，触发 AI 生成一句话总结 + 结构化纪要。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_meeting(db, meeting, body.device_id)

    # 合并转写
    await merge_transcript_to_meeting(db, meeting)
    meeting.status = MeetingStatus.ENDED
    meeting.actual_end = meeting.actual_end or _now()
    meeting.audio_duration = meeting.audio_duration or 0
    await db.commit()
    await db.refresh(meeting)

    minutes_row = await generate_minutes(db, meeting, user_id=0)
    return success_response(
        data=MinutesOut.model_validate(minutes_row).model_dump(mode="json"),
        msg="会议纪要生成成功" if minutes_row.status == "success" else "会议已结束",
    )


@router.get("/client/meetings/{meeting_id}")
async def client_get_meeting(
    meeting_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    device_id: str = Query(..., min_length=8),
):
    """用户端：查询会议详情（含转写进度、纪要状态）。"""
    meeting = await _get_meeting_or_404(db, meeting_id)
    await _check_meeting(db, meeting, device_id)

    minutes_row = (
        await db.execute(
            select(AimeetingMinutes).where(AimeetingMinutes.meeting_id == meeting.id)
        )
    ).scalars().first()

    await db.refresh(meeting)
    data = MeetingOut.model_validate(meeting).model_dump(mode="json")
    data["minutes"] = (
        MinutesOut.model_validate(minutes_row).model_dump(mode="json")
        if minutes_row
        else None
    )
    return success_response(data=data)


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

    data = MeetingOut.model_validate(meeting).model_dump(mode="json")
    data["record_count"] = record_count
    data["minutes_status"] = minutes_row.status if minutes_row else "none"
    data["minutes"] = (
        MinutesOut.model_validate(minutes_row).model_dump(mode="json")
        if minutes_row
        else None
    )
    return success_response(data=data)


@router.post("/meetings")
async def create_meeting(
    body: MeetingCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("aimeeting:meeting:create"))],
):
    """管理端：创建会议。"""
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
    meeting = AimeetingMeeting(
        title=body.title.strip(),
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
    """管理端：更新会议基本信息。"""
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