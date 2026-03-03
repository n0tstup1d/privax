from enum import Enum as PyEnum
from datetime import datetime
from typing import List, Optional
from sqlalchemy import ForeignKey, String, Float, Text, Enum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- БИЛЛИНГ И ТАРИФЫ ---

class ServicePlan(Base):
    """
    Тарифные планы. Один план = один период одного уровня.
    Например: Silver 1м, Silver 3м, Silver 6м — это три разных записи с tier_level=1.

    price          — базовая цена ЗА МЕСЯЦ в рублях
    months         — на сколько месяцев этот план (1, 3, 6...)
    discount_percent — скидка в % которая применяется к итоговой сумме
    
    Итоговая сумма считается так:
        base  = price * months
        final = base * (1 - discount_percent / 100)
    """
    __tablename__ = "service_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))           # "Silver 3 месяца" и т.д.
    tier_level: Mapped[int] = mapped_column(default=1)      # уровень серверов (1=Silver, 2=Gold...)
    price: Mapped[float] = mapped_column(Float)             # базовая цена за 1 месяц
    months: Mapped[int] = mapped_column(default=1)          # период подписки в месяцах
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)  # скидка в %
    max_sessions: Mapped[int] = mapped_column(default=3)    # лимит одновременных устройств

    # duration_days убрали — он всегда вычисляется как months * 30
    # если нужен в коде: plan.months * 30


class InvoiceStatus(PyEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"


class Invoice(Base):
    """
    Фиксирует любое движение денег.

    plan_id = None      → пополнение баланса
    plan_id = int       → покупка подписки
    topped_up_by = None → система (покупка) или платёжка (ЮKassa)
    topped_up_by = int  → id админа который пополнил вручную
    """
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
    """Главная таблица клиента"""
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    password: Mapped[str] = mapped_column(nullable=False)
    is_admin: Mapped[bool] = mapped_column(default=False)
    balance: Mapped[float] = mapped_column(default=0.0)
    referral_code: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)

    refresh_tokens: Mapped[List["RefreshToken"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    configs: Mapped[List["Config"]] = relationship(back_populates="owner")
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="client", foreign_keys="Invoice.client_id")
    notifications: Mapped[List["Notification"]] = relationship(back_populates="client")


class RefreshToken(Base):
    """Сессии пользователей"""
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(unique=True, index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))

    client: Mapped["Client"] = relationship(back_populates="refresh_tokens")


# --- VPN ИНФРАСТРУКТУРА ---

class VPNServer(Base):
    """Наши сервера Marzban"""
    __tablename__ = "vpn_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    country_code: Mapped[str] = mapped_column(String(5), default="DE")
    tier_level: Mapped[int] = mapped_column(default=1)
    ip_address: Mapped[str] = mapped_column(unique=True)
    ssh_port: Mapped[int] = mapped_column(default=22)

    marzban_port: Mapped[int] = mapped_column(default=8000)
    mar_admin_user: Mapped[str] = mapped_column()           # зашифровано через crypto_service
    mar_admin_pass: Mapped[str] = mapped_column()           # зашифровано через crypto_service

    current_users_count: Mapped[int] = mapped_column(default=0)   # текущее кол-во активных юзеров
    max_users: Mapped[int] = mapped_column(default=100)            # лимит — сколько юзеров можно посадить
    is_active: Mapped[bool] = mapped_column(default=True)

    # Reality параметры — заполняются автоматически при добавлении сервера
    reality_public_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reality_short_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON: ["id1", "id2"]
    server_names: Mapped[Optional[str]] = mapped_column(Text, nullable=True)         # JSON: ["domain1", "domain2"]

    configs: Mapped[List["Config"]] = relationship(back_populates="server")


class Config(Base):
    """
    Активная подписка клиента.
    Одна запись = один активный VPN-аккаунт на одном сервере.
    marzban_username — под этим именем клиент зарегистрирован в Marzban на сервере server_id
    subscription_url — ссылка/QR-код который клиент добавляет в VPN-приложение
    """
    __tablename__ = "configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("vpn_servers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("service_plans.id"))

    marzban_username: Mapped[str] = mapped_column(String(100), unique=True)
    user_uuid: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)        # UUID юзера в Marzban
    subscription_url: Mapped[Optional[str]] = mapped_column(Text)                       # запасная ссылка от Marzban
    vless_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)              # готовая VLESS Reality ссылка
    activation_code: Mapped[str] = mapped_column(String(50), unique=True)

    # Reality параметры этого конкретного клиента
    reality_short_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # личный shortId
    sub_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)  # токен для /sub/{token}

    expire_at: Mapped[datetime] = mapped_column()
    auto_renew: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)

    owner: Mapped["Client"] = relationship(back_populates="configs")
    server: Mapped["VPNServer"] = relationship(back_populates="configs")
    plan: Mapped["ServicePlan"] = relationship()


# --- ДОМЕНЫ-МАСКИ ---

class TrustedDomain(Base):
    __tablename__ = "trusted_domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    country_code: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)  # NULL = глобальный
    is_active: Mapped[bool] = mapped_column(default=True)
    added_by: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

# --- МАРКЕТИНГ И УВЕДОМЛЕНИЯ ---

class Promocode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)
    value: Mapped[float] = mapped_column()              # сколько рублей дарит
    max_usages: Mapped[int] = mapped_column(default=1)
    current_usages: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class Notification(Base):
    """Входящие сообщения в личном кабинете"""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    title: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    client: Mapped["Client"] = relationship(back_populates="notifications") 