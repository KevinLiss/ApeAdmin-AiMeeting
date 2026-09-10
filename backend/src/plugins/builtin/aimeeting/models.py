"""AI 会议助手插件——数据库模型。

表名约定：``aimeeting_`` 开头（插件名 aimeeting）。

核心工作流：
开始会议进行录音 → 语音转写（faster-whisper）→ LLM 总结 1 句话 → 结构化会议纪要输出
（结论、讨论要点、决议、遗留问题）

数据模型设计：
- ``aimeeting_meetings``：会议主表。用户端通过「会议编号 + 设备标识」访问；
  管理端用于管理历史会议、查看转写文本与纪要。
- ``aimeeting_records``：录音文件与语音转写结果。一次会议可有多次录音/转写片段，
  最终合并为完整转写文本。
- ``aimeeting_minutes``：AI 生成的会议总结（一句话）与结构化会议纪要（结论/讨论要点/决议/遗留问题）。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base
from src.models.mixins import IDMixin, TimestampMixin


class MeetingStatus:
    """会议状态常量。"""
    SCHEDULED = "scheduled"      # 待开始
    IN_PROGRESS = "in_progress"  # 进行中（已上传录音/转写中）
    ENDED = "ended"              # 已结束（纪要已生成）
    CANCELLED = "cancelled"      # 已取消


class TranscriptStatus:
    """转写状态常量。"""
    PENDING = "pending"     # 待转写
    PROCESSING = "processing"  # 转写中
    SUCCESS = "success"     # 转写完成
    FAILED = "failed"       # 转写失败


class AimeetingMeeting(IDMixin, TimestampMixin, Base):
    """AI 会议表。

    表名：``aimeeting_meetings``
    """

    __tablename__ = "aimeeting_meetings"

    # 会议基本信息（用户端创建时填写）
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="会议标题")
    # 会议编号（用户输入作为凭证查询会议）
    meeting_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True, comment="会议编号")
    # 参会人（用户填写，逗号分隔）
    participants: Mapped[str] = mapped_column(Text, default="", comment="参会人名单（逗号分隔）")
    # 会议时间
    start_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="会议开始时间（用户填写）"
    )
    end_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="会议结束时间"
    )

    # 会议状态
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MeetingStatus.SCHEDULED, comment="会议状态"
    )
    actual_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际开始录音时间"
    )
    actual_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际结束时间"
    )

    # 录音转写
    audio_file: Mapped[str] = mapped_column(String(500), default="", comment="录音文件路径（合并后的完整录音）")
    audio_duration: Mapped[int] = mapped_column(Integer, default=0, comment="录音总时长（秒）")
    transcript_text: Mapped[str] = mapped_column(Text, default="", comment="完整语音转写文本")
    transcript_status: Mapped[str] = mapped_column(
        String(20), default=TranscriptStatus.PENDING, comment="转写状态"
    )

    # 创建人（后台管理创建/用户端创建时记录）
    creator_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True, comment="创建人ID")
    creator_name: Mapped[str] = mapped_column(String(100), default="", comment="创建人名称")
    # 用户端无感登录设备标识
    device_id: Mapped[str] = mapped_column(String(200), default="", index=True, comment="设备标识（无感登录）")

    # 软删除
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, comment="软删除")


class AimeetingMinuteRecord(IDMixin, TimestampMixin, Base):
    """录音转写记录。

    表名：``aimeeting_records``
    一次会议可上传多段录音（支持暂停续录），每段独立转写，最终合并为会议完整转写文本。
    """

    __tablename__ = "aimeeting_records"

    meeting_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属会议ID")
    # 录音文件
    audio_path: Mapped[str] = mapped_column(String(500), default="", comment="录音文件路径")
    audio_duration: Mapped[int] = mapped_column(Integer, default=0, comment="录音时长（秒）")
    # 转写
    transcript: Mapped[str] = mapped_column(Text, default="", comment="本段转写文本")
    transcript_status: Mapped[str] = mapped_column(
        String(20), default=TranscriptStatus.PENDING, comment="本段转写状态"
    )
    error: Mapped[str] = mapped_column(Text, default="", comment="转写失败原因")
    # 设备
    device_id: Mapped[str] = mapped_column(String(200), default="", index=True, comment="设备标识")
    creator_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="上传人ID")
    creator_name: Mapped[str] = mapped_column(String(100), default="", comment="上传人名称")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, comment="软删除")


class AimeetingMinutes(IDMixin, TimestampMixin, Base):
    """AI 生成的会议总结与会议纪要。

    表名：``aimeeting_minutes``
    会议转写完成后由 AI 自动生成：一句话总结 + 结构化会议纪要。
    """

    __tablename__ = "aimeeting_minutes"

    meeting_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属会议ID")
    # 会议总结（LLM 总结 1 句话）
    summary: Mapped[str] = mapped_column(Text, default="", comment="会议总结（一句话）")
    # 结构化会议纪要：结论 / 讨论要点 / 决议 / 遗留问题
    minutes: Mapped[str] = mapped_column(Text, default="", comment="结构化会议纪要（Markdown）")
    # 生成原始素材（完整转写文本）
    source_material: Mapped[str] = mapped_column(Text, default="", comment="生成素材（完整转写文本）")
    # 生成状态：pending / success / failed
    status: Mapped[str] = mapped_column(String(20), default="pending", comment="生成状态")
    provider_name: Mapped[str] = mapped_column(String(100), default="", comment="使用的AI供应商")
    error: Mapped[str] = mapped_column(Text, default="", comment="生成失败原因")
    generated_by: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="触发生成用户ID")