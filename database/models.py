from enum import Enum as PyEnum
from datetime import datetime
from typing import List, Optional
from sqlalchemy import ForeignKey, String, Float, Text, Enum, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- ТИПЫ ИНБАУНДОВ ---

class InboundType(str, PyEnum):
    TCP_REALITY = "tcp_reality"  # VLESS + TCP + Reality


# --- ТИРЫ СЕРВЕРОВ ---

class ServerTier(Base):
    """
    Уровень (тир) серверов. Все свойства уровня хранятся здесь —
    серверы и планы просто ссылаются на тир по FK.

    level            — уникальный номер (1, 2, 3...), используется как FK
    default_max_users — сколько юзеров помещается на один сервер этого тира
    max_sessions     — максимум одновременных устройств у клиента этого тира
    speed_mbps       — декларируемая скорость для клиентов (0 = не показываем)
    priority         — приоритет обслуживания (чем выше — тем лучше)
    """
    __tablename__ = "server_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)        # "silver"
    display_name: Mapped[str] = mapped_column(String(100))            # "Silver"
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    default_max_users: Mapped[int] = mapped_column(default=100)
    max_sessions: Mapped[int] = mapped_column(default=3)
    speed_mbps: Mapped[int] = mapped_column(default=0)
    priority: Mapped[int] = mapped_column(default=1)

    servers: Mapped[List["VPNServer"]] = relationship(back_populates="tier", foreign_keys="VPNServer.tier_level")
    plans:   Mapped[List["ServicePlan"]] = relationship(back_populates="tier", foreign_keys="ServicePlan.tier_level")


# --- БИЛЛИНГ И ТАРИФЫ ---

class ServicePlan(Base):
    """
    Тарифные планы. Один план = один период одного уровня.
    Например: Silver 1м, Silver 3м, Silver 6м — три записи с tier_level=1.

    max_sessions и default_max_users берутся из ServerTier — здесь не хранятся.
    Итоговая цена: price * (duration_days / 30) * (1 - discount_percent / 100)
    """
    __tablename__ = "service_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    display_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tier_level: Mapped[int] = mapped_column(ForeignKey("server_tiers.level"))
    price: Mapped[float] = mapped_column(Float)
    duration_days: Mapped[int] = mapped_column(default=30)  # длительность в днях (3, 7, 30, 90, 180...)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)
    is_hidden: Mapped[bool] = mapped_column(default=False)  # скрыть от клиентов (не удалять)
    purchase_limit: Mapped[int] = mapped_column(default=0)  # 0 = безлимит, 1 = только один раз

    tier: Mapped["ServerTier"] = relationship(back_populates="plans", foreign_keys=[tier_level])


class InvoiceStatus(PyEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("service_plans.id"), nullable=True)
    topped_up_by: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus), default=InvoiceStatus.PENDING)
    external_id: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    client: Mapped["Client"] = relationship(back_populates="invoices", foreign_keys="Invoice.client_id")
    admin: Mapped[Optional["Client"]] = relationship(foreign_keys="Invoice.topped_up_by")
    plan: Mapped[Optional["ServicePlan"]] = relationship()


# --- ПОЛЬЗОВАТЕЛИ ---

class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    password: Mapped[str] = mapped_column(nullable=False)
    is_admin: Mapped[bool] = mapped_column(default=False)
    is_banned: Mapped[bool] = mapped_column(default=False)
    balance: Mapped[float] = mapped_column(default=0.0)
    referral_code: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)
    # ID реферера — кто пригласил этого пользователя
    referred_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), nullable=True)

    refresh_tokens: Mapped[List["RefreshToken"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    configs: Mapped[List["Config"]] = relationship(back_populates="owner")
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="client", foreign_keys="Invoice.client_id")
    notifications: Mapped[List["Notification"]] = relationship(back_populates="client")
    tickets:       Mapped[List["SupportTicket"]]  = relationship(back_populates="client")
    referrals:     Mapped[List["Referral"]]        = relationship(back_populates="referrer", foreign_keys="Referral.referrer_id")


class Referral(Base):
    """
    Запись о реферальном приглашении.
    referrer_id  — кто пригласил (владелец ссылки)
    referred_id  — кто пришёл по ссылке
    bonus_given  — был ли уже начислен бонус рефереру
    """
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    referred_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), unique=True)  # один юзер — один реферер
    bonus_given: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    bonus_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    referrer: Mapped["Client"] = relationship(back_populates="referrals", foreign_keys=[referrer_id])
    referred: Mapped["Client"] = relationship(foreign_keys=[referred_id])


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(unique=True, index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))

    client: Mapped["Client"] = relationship(back_populates="refresh_tokens")


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    blocked_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class PasswordResetCode(Base):
    __tablename__ = "password_reset_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    code: Mapped[str] = mapped_column(String(6))
    expires_at: Mapped[datetime] = mapped_column()
    is_used: Mapped[bool] = mapped_column(default=False)

    client: Mapped["Client"] = relationship()


# --- VPN ИНФРАСТРУКТУРА ---

class VPNServer(Base):
    """
    VPN-сервер на базе 3x-ui. Вместимость берётся из ServerTier.default_max_users.
    """
    __tablename__ = "vpn_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    country_code: Mapped[str] = mapped_column(String(5), default="DE")
    tier_level: Mapped[int] = mapped_column(ForeignKey("server_tiers.level"))
    ip_address: Mapped[str] = mapped_column(unique=True)
    ssh_port: Mapped[int] = mapped_column(default=22)

    # Порт 3x-ui панели (по умолчанию 2053, но у каждого сервера может быть свой)
    panel_port: Mapped[int] = mapped_column(default=2053)
    panel_path: Mapped[str] = mapped_column(String(100), default="")  # секретный путь панели (например /IowuXQyUA8bB)
    mar_admin_user: Mapped[str] = mapped_column()
    mar_admin_pass: Mapped[str] = mapped_column()

    current_users_count: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)

    inbound_type: Mapped[InboundType] = mapped_column(
        Enum(InboundType, values_callable=lambda x: [e.value for e in x]),
        default=InboundType.TCP_REALITY
    )

    reality_public_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reality_short_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    server_names: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    tier:    Mapped["ServerTier"] = relationship(back_populates="servers", foreign_keys=[tier_level])
    configs: Mapped[List["Config"]] = relationship(back_populates="server")


class Config(Base):
    __tablename__ = "configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("vpn_servers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("service_plans.id"))

    xui_username: Mapped[str] = mapped_column(String(100), unique=True)   # email в 3x-ui (напр. "privax_42_abc1")
    xui_uuid: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)   # UUID клиента в 3x-ui
    xui_inbound_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # ID инбаунда в 3x-ui
    xui_short_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # shortId в инбаунде Reality

    vless_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sub_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)

    expire_at: Mapped[datetime] = mapped_column()
    auto_renew: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    last_reset_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    owner:  Mapped["Client"]      = relationship(back_populates="configs")
    server: Mapped["VPNServer"]   = relationship(back_populates="configs")
    plan:   Mapped["ServicePlan"] = relationship()


# --- ДОМЕНЫ-МАСКИ ---

class TrustedDomain(Base):
    __tablename__ = "trusted_domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    country_code: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    added_by: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


# --- МАРКЕТИНГ И УВЕДОМЛЕНИЯ ---

class PromocodeType(PyEnum):
    BALANCE  = "balance"
    DISCOUNT = "discount"


class Promocode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)
    promo_type: Mapped[PromocodeType] = mapped_column(Enum(PromocodeType), default=PromocodeType.BALANCE)
    value: Mapped[float] = mapped_column(default=0.0)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("service_plans.id"), nullable=True)
    max_usages: Mapped[int] = mapped_column(default=1)
    max_usages_per_client: Mapped[int] = mapped_column(default=1)
    current_usages: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    plan:   Mapped[Optional["ServicePlan"]] = relationship()
    usages: Mapped[List["PromocodeUsage"]]  = relationship(back_populates="promocode", cascade="all, delete-orphan")


class PromocodeUsage(Base):
    __tablename__ = "promocode_usages"

    id: Mapped[int] = mapped_column(primary_key=True)
    promocode_id: Mapped[int] = mapped_column(ForeignKey("promocodes.id"))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    used_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    promocode: Mapped["Promocode"] = relationship(back_populates="usages")
    client:    Mapped["Client"]    = relationship()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    title: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    client: Mapped["Client"] = relationship(back_populates="notifications")

class FaqArticle(Base):
    """
    Статья FAQ. Контент хранится как Markdown в поле `content`.
    Картинки привязаны к статье через FaqImage.
    """
    __tablename__ = "faq_articles"

    id:           Mapped[int]      = mapped_column(primary_key=True)
    title:        Mapped[str]      = mapped_column(String(200))
    slug:         Mapped[str]      = mapped_column(String(200), unique=True, index=True)
    category:     Mapped[str]      = mapped_column(String(100), default="Общее")
    content:      Mapped[str]      = mapped_column(Text)
    is_published: Mapped[bool]     = mapped_column(default=False)
    created_at:   Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at:   Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

    images: Mapped[List["FaqImage"]] = relationship(back_populates="article", cascade="all, delete-orphan")


class FaqImage(Base):
    """Картинка привязанная к статье FAQ, хранится на диске."""
    __tablename__ = "faq_images"

    id:          Mapped[int] = mapped_column(primary_key=True)
    article_id:  Mapped[int] = mapped_column(ForeignKey("faq_articles.id"))
    filename:    Mapped[str] = mapped_column(String(200))
    stored_name: Mapped[str] = mapped_column(String(200))
    mime_type:   Mapped[str] = mapped_column(String(50), default="image/png")

    article: Mapped["FaqArticle"] = relationship(back_populates="images")


class SupportTicket(Base):
    """Обращение пользователя: вопрос, жалоба, предложение."""
    __tablename__ = "support_tickets"

    id:         Mapped[int]      = mapped_column(primary_key=True)
    client_id:  Mapped[int]      = mapped_column(ForeignKey("clients.id"))
    type:       Mapped[str]      = mapped_column(String(20))   # question / complaint / suggestion
    subject:    Mapped[str]      = mapped_column(String(200))
    message:    Mapped[str]      = mapped_column(Text)
    status:     Mapped[str]      = mapped_column(String(20), default="open")  # open / answered / closed
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

    client:  Mapped["Client"]          = relationship(back_populates="tickets")
    replies: Mapped[List["TicketReply"]] = relationship(back_populates="ticket", cascade="all, delete-orphan", order_by="TicketReply.created_at")
    attachments: Mapped[List["TicketAttachment"]] = relationship(back_populates="ticket", cascade="all, delete-orphan", foreign_keys="TicketAttachment.ticket_id")


class TicketReply(Base):
    """Ответ на обращение (от пользователя или от администратора)."""
    __tablename__ = "ticket_replies"

    id:         Mapped[int]      = mapped_column(primary_key=True)
    ticket_id:  Mapped[int]      = mapped_column(ForeignKey("support_tickets.id"))
    is_admin:   Mapped[bool]     = mapped_column(default=False)
    message:    Mapped[str]      = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    ticket: Mapped["SupportTicket"] = relationship(back_populates="replies")
    attachments: Mapped[List["TicketAttachment"]] = relationship(back_populates="reply", cascade="all, delete-orphan", foreign_keys="TicketAttachment.reply_id")


class TicketAttachment(Base):
    """Файл/фото, прикреплённый к тикету или к ответу."""
    __tablename__ = "ticket_attachments"

    id:         Mapped[int]           = mapped_column(primary_key=True)
    ticket_id:  Mapped[int]           = mapped_column(ForeignKey("support_tickets.id"), nullable=True)
    reply_id:   Mapped[Optional[int]] = mapped_column(ForeignKey("ticket_replies.id"), nullable=True)
    filename:   Mapped[str]           = mapped_column(String(255))
    stored_name:Mapped[str]           = mapped_column(String(255))   # UUID-имя на диске
    mime_type:  Mapped[str]           = mapped_column(String(100))
    size:       Mapped[int]           = mapped_column(Integer)        # bytes
    created_at: Mapped[datetime]      = mapped_column(default=datetime.utcnow)

    ticket: Mapped[Optional["SupportTicket"]] = relationship(back_populates="attachments", foreign_keys=[ticket_id])
    reply:  Mapped[Optional["TicketReply"]]   = relationship(back_populates="attachments", foreign_keys=[reply_id])