"""AI 会议助手插件——MCP 工具注册。

暴露给 AI Agent 调用，让 AI 能查询会议列表、会议记录。
"""
import json

from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal
from src.mcp.manager import mcp_manager
from src.plugins.builtin.aimeeting.models import AimeetingMeeting, AimeetingMinuteRecord

PLUGIN_NAME = "aimeeting"


async def _list_meetings(status: str = "", page: int = 1, page_size: int = 10) -> str:
    """列出会议列表，可按状态筛选。

    Args:
        status: 会议状态（scheduled/in_progress/ended/cancelled），留空为全部。
        page: 页码，默认 1。
        page_size: 每页数量，默认 10，最大 50。
    """
    page = max(1, page)
    page_size = min(50, max(1, page_size))
    async with SessionLocal() as db:
        from src.plugins.builtin.aimeeting.models import MeetingStatus

        stmt = select(AimeetingMeeting).where(AimeetingMeeting.is_deleted == False)  # noqa: E712
        if status:
            stmt = stmt.where(AimeetingMeeting.status == status)
        count_stmt = stmt
        total = len((await db.execute(count_stmt)).scalars().all())
        stmt = stmt.order_by(AimeetingMeeting.id.desc()).offset((page - 1) * page_size).limit(page_size)
        meetings = (await db.execute(stmt)).scalars().all()

    return json.dumps({
        "total": total,
        "items": [
            {
                "id": m.id,
                "title": m.title,
                "meeting_type": m.meeting_type,
                "status": m.status,
                "organizer": m.organizer,
                "start_time": m.start_time.strftime("%Y-%m-%d %H:%M") if m.start_time else None,
                "participants": m.participants,
            }
            for m in meetings
        ],
    }, ensure_ascii=False)


async def _list_records(meeting_id: int) -> str:
    """列出某次会议的实时记录（按时间线排序）。

    Args:
        meeting_id: 会议ID。
    """
    async with SessionLocal() as db:
        stmt = (
            select(AimeetingMinuteRecord)
            .where(
                AimeetingMinuteRecord.meeting_id == meeting_id,
                AimeetingMinuteRecord.is_deleted == False,  # noqa: E712
            )
            .order_by(AimeetingMinuteRecord.record_time.asc(), AimeetingMinuteRecord.id.asc())
        )
        records = (await db.execute(stmt)).scalars().all()

    return json.dumps({
        "meeting_id": meeting_id,
        "total": len(records),
        "items": [
            {
                "id": r.id,
                "speaker": r.speaker,
                "content": r.content,
                "record_time": r.record_time.strftime("%Y-%m-%d %H:%M:%S") if r.record_time else None,
                "creator_name": r.creator_name,
            }
            for r in records
        ],
    }, ensure_ascii=False)


def register_aimeeting_mcp_tools() -> None:
    """注册 MCP 工具。在 ``plugin.py`` 的 ``register_mcp_tools()`` 中调用。"""
    mcp_manager.register_tool(
        name="aimeeting_list_meetings",
        description="列出所有 AI 会议（支持按状态筛选），返回会议基本信息和状态。",
        handler=_list_meetings,
        required_permissions=["aimeeting:meeting:list"],
        plugin_name=PLUGIN_NAME,
        category=PLUGIN_NAME,
    )
    mcp_manager.register_tool(
        name="aimeeting_list_records",
        description="获取某次会议的实时记录列表（时间线排序）。",
        handler=_list_records,
        required_permissions=["aimeeting:record:list"],
        plugin_name=PLUGIN_NAME,
        category=PLUGIN_NAME,
    )
    logger.info("[aimeeting] Registered 2 MCP tools")