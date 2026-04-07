from enum import Enum as PyEnum
from datetime import datetime
from typing import List, Optional
from sqlalchemy import ForeignKey, String, Float, Text, Enum, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- ТИРЫ СЕРВЕРОВ ---

class ServerTier(Base):
    """
    Уровень (тир) серверов. Все свойства уровня хранятся здесь.

    level            — уникальный номер (1, 2, 3...), используется как FK
    default_max_users — сколько юзеров помещается на один сервер этого тира
    max_sessions     — максимум одновременных подписок у клиента этого тира
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
    Итоговая цена: price * (1 - discount_percent / 100)
    """
    __tablename__ = "service_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    display_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tier_level: Mapped[int] = mapped_column(ForeignKey("server_tiers.level"))
    price: Mapped[float] = mapped_column(Float)
    duration_days: Mapped[int] = mapped_column(default=30)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)
    is_hidden: Mapped[bool] = mapped_column(default=False)
    purchase_limit: Mapped[int] = mapped_column(default=0)  # 0 = безлимит

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
    referred_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), nullable=True)

    refresh_tokens: Mapped[List["RefreshToken"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    configs: Mapped[List["Config"]] = relationship(back_populates="owner")
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="client", foreign_keys="Invoice.client_id")
    notifications: Mapped[List["Notification"]] = relationship(back_populates="client")
    tickets:       Mapped[List["SupportTicket"]]  = relationship(back_populates="client")
    referrals:     Mapped[List["Referral"]]        = relationship(back_populates="referrer", foreign_keys="Referral.referrer_id")


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    referred_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), unique=True)
    bonus_given: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    bonus_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    referrer: Mapped["Client"] = relationship(back_populates="referrals", foreign_keys=[referrer_id])
    referred: Mapped["Client"] = relationship(foreign_keys=[referred_id])


class ReferralCode(Base):
    """
    Индивидуальные настройки реферального кода.
    Создаётся при первом запросе пользователя к /referral/me.
    Переопределяет глобальные настройки из ReferralSettings (если поле не None).
    """
    __tablename__ = "referral_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), unique=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    # Переопределения (None = использовать глобальные настройки)
    bonus_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    referred_discount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)  # можно деактивировать конкретную ссылку
    max_bonus_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

    client: Mapped["Client"] = relationship(foreign_keys=[client_id])


class ReferralSettings(Base):
    """
    Глобальные настройки реферальной программы (дефолты для новых кодов).
    Всегда одна запись (id=1).
    """
    __tablename__ = "referral_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    bonus_days: Mapped[int] = mapped_column(Integer, default=14)
    referred_discount: Mapped[float] = mapped_column(Float, default=10.0)
    is_active: Mapped[bool] = mapped_column(default=True)
    min_purchase_amount: Mapped[float] = mapped_column(Float, default=0.0)
    max_bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    # Лимит регистраций по одной реферальной ссылке. 0 = без лимита.
    invite_limit: Mapped[int] = mapped_column(Integer, default=5)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)


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


# --- СЕРВЕРНАЯ ИНФРАСТРУКТУРА ---

class VPNServer(Base):
    """
    Сервер на базе Marzban.

    Основное отличие от 3x-ui:
    - marzban_url    — полный URL панели (https://my-server.com:8000)
    - mar_admin_user — логин администратора Marzban (зашифрован)
    - mar_admin_pass — пароль администратора Marzban (зашифрован)
    - inbounds_json  — JSON-строка доступных inbounds {"vless": ["VLESS TCP REALITY"]}
    - Нет SSH-полей, нет xui-специфики
    """
    __tablename__ = "vpn_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    country_code: Mapped[str] = mapped_column(String(5), default="DE")
    tier_level: Mapped[int] = mapped_column(ForeignKey("server_tiers.level"))
    ip_address: Mapped[str] = mapped_column(unique=True)

    # Marzban API — основной способ управления
    marzban_url: Mapped[str] = mapped_column(String(255))          # https://host:port
    mar_admin_user: Mapped[str] = mapped_column()                   # зашифровано
    mar_admin_pass: Mapped[str] = mapped_column()                   # зашифровано

    # Доступные inbounds на этом сервере (JSON)
    # Пример: '{"vless": ["VLESS TCP REALITY"], "trojan": ["Trojan WS TLS"]}'
    inbounds_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Reality параметры — нужны для генерации ссылок
    reality_public_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reality_short_ids:  Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON список
    reality_sni:        Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Тип сервера: marzban (VLESS/Trojan), wg-easy (WireGuard), outline (SS)
    server_type: Mapped[str] = mapped_column(String(20), default="marzban")

    current_users_count: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)

    # Статус доступности — обновляется фоновой задачей каждые 5 минут
    is_online: Mapped[bool] = mapped_column(default=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    tier:    Mapped["ServerTier"] = relationship(back_populates="servers", foreign_keys=[tier_level])
    configs: Mapped[List["Config"]] = relationship(back_populates="server")


class Config(Base):
    """
    Конфигурация подписки клиента.

    Для Marzban:
    - mar_username — username пользователя в Marzban (уникальный, напр. "privax_42_abc1")
    - subscription_url — URL подписки от Marzban (/sub/<token>)
    - vless_link — одна из VLESS-ссылок (для отображения в UI)
    - Нет xui_uuid, xui_inbound_id, xui_short_id — они не нужны в Marzban
    """
    __tablename__ = "configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("vpn_servers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("service_plans.id"))

    # Marzban идентификатор пользователя
    mar_username: Mapped[str] = mapped_column(String(100), unique=True)

    # Ссылки
    subscription_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # URL подписки Marzban
    vless_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # первая VLESS-ссылка

    # Наш токен подписки (для /sub/<token> эндпоинта нашего API)
    sub_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)

    expire_at: Mapped[datetime] = mapped_column()
    auto_renew: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    last_reset_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # Флаги уведомлений — сбрасываются при продлении/покупке
    notified_24h: Mapped[bool] = mapped_column(default=False)
    notified_3h:  Mapped[bool] = mapped_column(default=False)

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


class NotificationType(PyEnum):
    EXPIRY_24H   = "expiry_24h"
    EXPIRY_3H    = "expiry_3h"
    AUTO_RENEW   = "auto_renew"
    LOW_BALANCE  = "low_balance"
    EXPIRED      = "expired"
    MIGRATED     = "migrated"       # подписка перенесена на другой сервер
    LINK_CHANGED = "link_changed"   # ссылка обновилась (reconfigure, перенос)
    PURCHASED    = "purchased"      # покупка подписки
    RESET        = "reset"          # сброс устройств


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    config_id: Mapped[Optional[int]] = mapped_column(ForeignKey("configs.id"), nullable=True)
    notification_type: Mapped[Optional[NotificationType]] = mapped_column(Enum(NotificationType), nullable=True)
    title: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    client: Mapped["Client"] = relationship(back_populates="notifications")


class FaqArticle(Base):
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
    __tablename__ = "faq_images"

    id:          Mapped[int] = mapped_column(primary_key=True)
    article_id:  Mapped[int] = mapped_column(ForeignKey("faq_articles.id"))
    filename:    Mapped[str] = mapped_column(String(200))
    stored_name: Mapped[str] = mapped_column(String(200))
    mime_type:   Mapped[str] = mapped_column(String(50), default="image/png")

    article: Mapped["FaqArticle"] = relationship(back_populates="images")


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id:         Mapped[int]      = mapped_column(primary_key=True)
    client_id:  Mapped[int]      = mapped_column(ForeignKey("clients.id"))
    type:       Mapped[str]      = mapped_column(String(20))
    subject:    Mapped[str]      = mapped_column(String(200))
    message:    Mapped[str]      = mapped_column(Text)
    status:     Mapped[str]      = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

    client:  Mapped["Client"]          = relationship(back_populates="tickets")
    replies: Mapped[List["TicketReply"]] = relationship(back_populates="ticket", cascade="all, delete-orphan", order_by="TicketReply.created_at")
    attachments: Mapped[List["TicketAttachment"]] = relationship(back_populates="ticket", cascade="all, delete-orphan", foreign_keys="TicketAttachment.ticket_id")


class TicketReply(Base):
    __tablename__ = "ticket_replies"

    id:         Mapped[int]      = mapped_column(primary_key=True)
    ticket_id:  Mapped[int]      = mapped_column(ForeignKey("support_tickets.id"))
    is_admin:   Mapped[bool]     = mapped_column(default=False)
    message:    Mapped[str]      = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    ticket: Mapped["SupportTicket"] = relationship(back_populates="replies")
    attachments: Mapped[List["TicketAttachment"]] = relationship(back_populates="reply", cascade="all, delete-orphan", foreign_keys="TicketAttachment.reply_id")


# --- АДМИН РОЛИ ---

class AdminRole(PyEnum):
    OWNER     = "owner"      # полный доступ, управление другими админами
    DEVELOPER = "developer"  # серверы, логи, техническая часть
    OPERATOR  = "operator"   # клиенты, тикеты, промокоды, уведомления


class AdminProfile(Base):
    """
    Профиль администратора — отдельная таблица от clients.

    Логика:
    - Любой Client может стать админом — добавляем запись сюда
    - is_admin в Client больше не используется для проверки прав,
      только AdminProfile определяет роль и доступ
    - created_by — кто выдал доступ (всегда owner)
    - is_active  — можно отозвать доступ не удаляя запись (история сохраняется)
    """
    __tablename__ = "admin_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), unique=True)
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole), default=AdminRole.OPERATOR)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # заметка о сотруднике

    client:            Mapped["Client"]          = relationship(foreign_keys=[client_id])
    created_by_client: Mapped[Optional["Client"]] = relationship(foreign_keys=[created_by])


# --- АДМИН 2FA ---

class AdminOTPCode(Base):
    """
    Одноразовый код для входа в админку через Telegram.
    pending_token — временный токен выданный после проверки пароля.
    После успешной проверки кода — удаляется.
    """
    __tablename__ = "admin_otp_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    pending_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code: Mapped[str] = mapped_column(String(6))
    expires_at: Mapped[datetime] = mapped_column()
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    is_used: Mapped[bool] = mapped_column(default=False)

    client: Mapped["Client"] = relationship()


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"

    id:         Mapped[int]           = mapped_column(primary_key=True)
    ticket_id:  Mapped[int]           = mapped_column(ForeignKey("support_tickets.id"), nullable=True)
    reply_id:   Mapped[Optional[int]] = mapped_column(ForeignKey("ticket_replies.id"), nullable=True)
    filename:   Mapped[str]           = mapped_column(String(255))
    stored_name:Mapped[str]           = mapped_column(String(255))
    mime_type:  Mapped[str]           = mapped_column(String(100))
    size:       Mapped[int]           = mapped_column(Integer)
    created_at: Mapped[datetime]      = mapped_column(default=datetime.utcnow)

    ticket: Mapped[Optional["SupportTicket"]] = relationship(back_populates="attachments", foreign_keys=[ticket_id])
    reply:  Mapped[Optional["TicketReply"]]   = relationship(back_populates="attachments", foreign_keys=[reply_id])


# --- ИСТОРИЯ ПОДПИСКИ ---

class SubscriptionEventType(PyEnum):
    PURCHASED    = "purchased"      # куплена
    RENEWED      = "renewed"        # продлена вручную
    AUTO_RENEWED = "auto_renewed"   # авто-продление
    RESET        = "reset"          # сброс устройств
    MIGRATED     = "migrated"       # перенос на другой сервер
    EXPIRED      = "expired"        # истекла
    DEACTIVATED  = "deactivated"    # деактивирована (вручную или принудительно)
    LINK_CHANGED = "link_changed"   # ссылка обновилась
    REACTIVATED  = "reactivated"    # восстановлена


class SubscriptionEvent(Base):
    """
    Лог событий подписки — полная история что происходило.
    Привязан к Config. Показывается клиенту и в админке.
    """
    __tablename__ = "subscription_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    config_id: Mapped[int] = mapped_column(ForeignKey("configs.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    event_type: Mapped[SubscriptionEventType] = mapped_column(Enum(SubscriptionEventType))
    description: Mapped[str] = mapped_column(Text)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Кто инициировал: null = система, иначе admin client_id
    initiated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, server_default=func.now())

    config: Mapped["Config"] = relationship(foreign_keys=[config_id])
    client: Mapped["Client"] = relationship(foreign_keys=[client_id])
    initiator: Mapped[Optional["Client"]] = relationship(foreign_keys=[initiated_by])


# --- АУДИТ-ЛОГ АДМИНОВ ---

class AdminActionType(PyEnum):
    # Серверы
    SERVER_ADD        = "server_add"
    SERVER_DELETE     = "server_delete"
    SERVER_UPDATE     = "server_update"
    SERVER_HARDEN     = "server_harden"
    SERVER_RECONFIGURE = "server_reconfigure"

    # Клиенты
    CLIENT_BAN        = "client_ban"
    CLIENT_UNBAN      = "client_unban"
    CLIENT_TOPUP      = "client_topup"
    CLIENT_SUB_MIGRATE = "client_sub_migrate"
    CLIENT_SUB_DELETE  = "client_sub_delete"

    # Тарифы
    PLAN_CREATE       = "plan_create"
    PLAN_UPDATE       = "plan_update"
    PLAN_DELETE        = "plan_delete"

    # Тиры
    TIER_CREATE       = "tier_create"
    TIER_UPDATE       = "tier_update"
    TIER_DELETE        = "tier_delete"

    # Промокоды
    PROMO_CREATE      = "promo_create"
    PROMO_DELETE      = "promo_delete"

    # Уведомления
    NOTIFICATION_SEND = "notification_send"

    # Рефералы
    REFERRAL_SETTINGS = "referral_settings"

    # Команда
    MANAGER_ADD       = "manager_add"
    MANAGER_REMOVE    = "manager_remove"
    MANAGER_UPDATE    = "manager_update"

    # Тикеты
    TICKET_REPLY      = "ticket_reply"
    TICKET_CLOSE      = "ticket_close"

    # Прочее
    OTHER             = "other"


class AdminAuditLog(Base):
    """
    Аудит-лог — каждое действие админа записывается.
    Кто, что, когда, с какого IP, какие детали.
    """
    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    action: Mapped[AdminActionType] = mapped_column(Enum(AdminActionType))
    description: Mapped[str] = mapped_column(Text)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Объект действия (опционально)
    target_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)   # "server", "client", "plan"...
    target_id: Mapped[Optional[int]] = mapped_column(nullable=True)                  # ID объекта

    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, server_default=func.now())

    admin: Mapped["Client"] = relationship(foreign_keys=[admin_id])


# --- УВЕДОМЛЕНИЯ АДМИНОВ ---

class AdminNotifType(PyEnum):
    SERVER_DOWN      = "server_down"        # сервер недоступен
    SERVER_UP        = "server_up"          # сервер снова онлайн
    RENEW_FAILED     = "renew_failed"       # авто-продление не прошло
    NEW_REGISTRATION = "new_registration"   # новый клиент
    NEW_TICKET       = "new_ticket"         # новый тикет поддержки
    SUB_PURCHASED    = "sub_purchased"      # клиент купил подписку
    BALANCE_TOPUP    = "balance_topup"      # пополнение баланса
    SYSTEM           = "system"             # системное


class AdminNotification(Base):
    """
    Уведомления для админов — отдельная лента от клиентских.
    Показываются в колокольчике в навбаре админки.
    """
    __tablename__ = "admin_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    notif_type: Mapped[AdminNotifType] = mapped_column(Enum(AdminNotifType))
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(default=False)

    # Связь с объектом (опционально)
    target_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_id: Mapped[Optional[int]] = mapped_column(nullable=True)

    # Для каких ролей (null = для всех)
    for_role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, server_default=func.now())


# --- ГАЙДЫ И ПРИЛОЖЕНИЯ ---

class GuidePlatform(Base):
    """
    Платформа (iOS, Android, Windows...).
    Отображается на странице Apps и Guides.
    """
    __tablename__ = "guide_platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(30), unique=True, index=True)       # "ios", "android"
    label: Mapped[str] = mapped_column(String(100))                              # "iPhone & iPad"
    subtitle: Mapped[str] = mapped_column(String(200), default="")              # "iOS 14+"
    emoji: Mapped[str] = mapped_column(String(10), default="📱")                # для Guides tabs
    icon_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True) # CDN URL иконки (simple-icons)
    color: Mapped[str] = mapped_column(String(20), default="#ffffff")            # hex цвет
    category: Mapped[str] = mapped_column(String(30), default="smartphones")    # smartphones / desktop / tv
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_visible: Mapped[bool] = mapped_column(default=True)

    apps: Mapped[List["GuideApp"]] = relationship(
        back_populates="platform", cascade="all, delete-orphan",
        order_by="GuideApp.sort_order",
    )


class GuideApp(Base):
    """
    Приложение для платформы (V2RayTun, Happ, V2RayN...).
    Одна платформа может иметь несколько приложений.
    """
    __tablename__ = "guide_apps"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("guide_platforms.id"))
    name: Mapped[str] = mapped_column(String(100))                               # "V2RayTun"
    store_name: Mapped[str] = mapped_column(String(100), default="")             # "App Store", "Google Play"
    download_url: Mapped[str] = mapped_column(String(500))                       # ссылка на скачивание
    is_primary: Mapped[bool] = mapped_column(default=False)                      # основное приложение для карточки Apps
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    platform: Mapped["GuidePlatform"] = relationship(back_populates="apps")
    steps: Mapped[List["GuideStep"]] = relationship(
        back_populates="app", cascade="all, delete-orphan",
        order_by="GuideStep.sort_order",
    )


class GuideStep(Base):
    """
    Шаг инструкции для приложения.
    """
    __tablename__ = "guide_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("guide_apps.id"))
    text: Mapped[str] = mapped_column(Text)                                      # "Скачайте V2RayTun из App Store"
    hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)             # доп. подсказка
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    app: Mapped["GuideApp"] = relationship(back_populates="steps")