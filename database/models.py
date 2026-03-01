from enum import Enum as PyEnum
from datetime import datetime
from typing import List, Optional
from sqlalchemy import ForeignKey, String, BigInteger, Float, DateTime, Boolean, Text, Enum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- БИЛЛИНГ И ТАРИФЫ ---

class ServicePlan(Base):
    """Тарифные планы: Silver, Gold, Elite"""
    __tablename__ = "service_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    tier_level: Mapped[int] = mapped_column(default=1)    # Уровень серверов
    price: Mapped[float] = mapped_column(Float)            # Цена
    max_sessions: Mapped[int] = mapped_column(default=3)  # Лимит устройств
    duration_days: Mapped[int] = mapped_column(default=30)


class InvoiceStatus(PyEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"


class Invoice(Base):
    """Счета на оплату"""
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("service_plans.id"))
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus), default=InvoiceStatus.PENDING)
    external_id: Mapped[Optional[str]] = mapped_column(String(100))  # ID от ЮKassa/Stripe
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    client: Mapped["Client"] = relationship(back_populates="invoices")
    plan: Mapped["ServicePlan"] = relationship()


# --- ПОЛЬЗОВАТЕЛИ ---

class Client(Base):
    """Главная таблица клиента"""
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    password: Mapped[str] = mapped_column(nullable=False)
    is_admin: Mapped[bool] = mapped_column(default=False)         # нужен для deps.py
    balance: Mapped[float] = mapped_column(default=0.0)
    referral_code: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)

    # Связи
    refresh_tokens: Mapped[List["RefreshToken"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    configs: Mapped[List["Config"]] = relationship(back_populates="owner")
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="client")
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
    tier_level: Mapped[int] = mapped_column(default=1)  # 1-обычный, 2-быстрый и т.д.
    ip_address: Mapped[str] = mapped_column(unique=True)
    ssh_port: Mapped[int] = mapped_column(default=22)   # на случай нестандартного порта

    # Доступы к API Marzban (зашифрованы через crypto_service)
    marzban_port: Mapped[int] = mapped_column(default=8000)
    mar_admin_user: Mapped[str] = mapped_column()
    mar_admin_pass: Mapped[str] = mapped_column()

    current_users_count: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)

    configs: Mapped[List["Config"]] = relationship(back_populates="server")


class Config(Base):
    """Подписка клиента — персональное шифрование"""
    __tablename__ = "configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("vpn_servers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("service_plans.id"))

    marzban_username: Mapped[str] = mapped_column(String(100), unique=True)  # имя юзера в Marzban
    subscription_url: Mapped[Optional[str]] = mapped_column(Text)            # ссылка для QR-кода
    activation_code: Mapped[str] = mapped_column(String(50), unique=True)

    # Логика работы
    expire_at: Mapped[datetime] = mapped_column()
    auto_renew: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)

    owner: Mapped["Client"] = relationship(back_populates="configs")
    server: Mapped["VPNServer"] = relationship(back_populates="configs")
    plan: Mapped["ServicePlan"] = relationship()


# --- МАРКЕТИНГ И УВЕДОМЛЕНИЯ ---

class Promocode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)
    value: Mapped[float] = mapped_column()                                        # Сколько денег дарит
    max_usages: Mapped[int] = mapped_column(default=1)
    current_usages: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)         # Срок действия


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