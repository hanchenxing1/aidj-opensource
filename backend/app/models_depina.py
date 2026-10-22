"""depina DePIN P2P 网络节点 + 加速每日配额模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DepinaNode(Base):
    """DePIN P2P 加速节点注册表（客户端 nodeB 向中心注册）。"""

    __tablename__ = "depina_nodes"

    peer_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    listen_addrs: Mapped[str] = mapped_column(Text, default="")
    proxy_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_hosts: Mapped[str] = mapped_column(Text, default="")
    # ISO 时间戳
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # 贡献统计
    served_bytes: Mapped[int] = mapped_column(Integer, default=0)
    served_peers: Mapped[int] = mapped_column(Integer, default=0)
    # 用户关联（可空，未开通账号的节点为 NULL；绑号后指向 users.id）
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 累计贡献奖励（分），与 users.balance_fen 入账一致，便于节点侧自查
    reward_fen: Mapped[int] = mapped_column(Integer, default=0)


class DepinaAccelUsage(Base):
    """DePIN 加速每日流量用量（按 user + 日期 唯一），用于配额硬限制。

    订阅用户（plus/pro）有 accel_gb_per_day 每日流量额度；超配额时：
    - 订阅用户：拒绝（403 提示额度用完，次日恢复）
    - 余额用户：按 payg 扣费（超量部分 1分/KB，扣不动则拒绝）
    """

    __tablename__ = "depina_accel_usage"
    __table_args__ = (
        {"sqlite_autoincrement": 0},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # UTC 日（避免时区跨日歧义）
    day: Mapped[datetime] = mapped_column(Date)
    # 当日累计反代流量（字节）
    served_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # 当日因超配额被 payg 扣费的部分（字节）
    payg_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # 当日 payg 扣费金额（分）
    payg_fen: Mapped[int] = mapped_column(Integer, default=0)
