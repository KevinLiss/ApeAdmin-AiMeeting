"""AI 会议助手插件——插件入口类。

功能：
- 会议创建与管理（线上/线下）
- 会议过程实时记录登记
- 会议结束后 AI 自动生成会议总结与会议纪要
"""
from loguru import logger

from fastapi import FastAPI

from src.core.config import settings
from src.db import Base, SessionLocal, engine
from src.plugins import PluginInterface
from src.plugins.builtin.aimeeting.models import (  # noqa: F401 — register ORM metadata
    AimeetingMeeting,
    AimeetingMinuteRecord,
    AimeetingMinutes,
)
from src.plugins.builtin.aimeeting.seed import seed_aimeeting_data


class AimeetingPlugin(PluginInterface):
    """AI 会议助手插件。"""

    name = "aimeeting"
    display_name = "AI 会议助手"
    description = "录音转写 + AI 会议纪要：开始会议进行录音 → 语音转写 → LLM 一句话总结 → 结构化会议纪要（结论/讨论要点/决议/遗留问题）"
    version = "1.1.0"
    author = "ApeAdmin"

    # ── 生命周期：安装（建表 + seed）──────────────────────
    async def install(self) -> None:
        """创建插件表 + seed 菜单权限。"""
        from src.models import User  # noqa: F401  register sys_user before FK resolution
        from src.models.mixins import IDMixin, TimestampMixin  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(
                    sync_conn,
                    tables=[
                        AimeetingMeeting.__table__,
                        AimeetingMinuteRecord.__table__,
                        AimeetingMinutes.__table__,
                    ],
                )
            )

        async with SessionLocal() as db:
            await seed_aimeeting_data(db)
            await db.commit()
        logger.info("[aimeeting] installed — tables ready, menus seeded")

    # ── 生命周期：卸载（清理）────────────────────────
    async def uninstall(self) -> None:
        """删除插件表与菜单。"""
        from sqlalchemy import delete

        from src.models import Menu

        async with SessionLocal() as db:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.drop_all(
                        sync_conn,
                        tables=[
                            AimeetingMinutes.__table__,
                            AimeetingMinuteRecord.__table__,
                            AimeetingMeeting.__table__,
                        ],
                    )
                )
            # 清理菜单
            await db.execute(delete(Menu).where(Menu.permission.like("aimeeting:%")))
            await db.execute(delete(Menu).where(Menu.path == "/aimeeting"))
            await db.commit()
        logger.info("[aimeeting] uninstalled — tables dropped, menus removed")

    # ── 生命周期：注册路由 + MCP 工具 ───────────────────
    def register(self, app: FastAPI) -> None:
        from src.plugins.builtin.aimeeting.api import router

        app.include_router(router, prefix=settings.API_PREFIX)
        logger.info("[aimeeting] registered — routes mounted")

    def register_mcp_tools(self) -> None:
        from src.plugins.builtin.aimeeting.mcp_tools import register_aimeeting_mcp_tools

        register_aimeeting_mcp_tools()