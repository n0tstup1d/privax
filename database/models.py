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

    price            — базовая цена ЗА МЕСЯЦ в рублях
    months           — на сколько месяцев этот план (1, 3, 6...)
    discount_percent — скидка в % которая применяется к итоговой сумме
    data_limit_gb    — лимит трафика в ГБ за весь период (0 = безлимит)

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
    data_limit_gb: Mapped[int] = mapped_column(default=0)   # лимит трафика в ГБ (0 = безлимит)


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


class LoginAttempt(Base):
    """
    Журнал неудачных попыток входа для антибрутфорс защиты.

    Логика:
        - Считаем попытки за последние BRUTE_WINDOW минут
        - Если >= MAX_ATTEMPTS — блокируем до blocked_until
        - После blocked_until счётчик сбрасывается автоматически
    """
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    blocked_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class PasswordResetCode(Base):
    """
    Одноразовый 6-значный код для сброса пароля.
    Действует RESET_TTL минут, после использования удаляется.
    Сейчас код печатается в консоль — когда подключишь email,
    просто замени print() на вызов email_service.
    """
    __tablename__ = "password_reset_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    code: Mapped[str] = mapped_column(String(6))
    expires_at: Mapped[datetime] = mapped_column()
    is_used: Mapped[bool] = mapped_column(default=False)

    client: Mapped["Client"] = relationship()


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

    marzban_username — имя юзера в Marzban (нужно для удаления/отключения)
    vless_link       — готовая ссылка от Marzban, пишется в БД как статичный снапшот
    sub_token        — токен для /sub/{token}, через который клиент получает актуальную ссылку
    """
    __tablename__ = "configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("vpn_servers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("service_plans.id"))

    marzban_username: Mapped[str] = mapped_column(String(100), unique=True)
    vless_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # ссылка от Marzban
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

class PromocodeType(PyEnum):
    BALANCE  = "balance"   # зачисляет рубли на баланс
    DISCOUNT = "discount"  # скидка % при покупке подписки

 
class Promocode(Base):
    """
    Промокод.

    Типы:
        balance  — зачисляет value рублей на баланс клиента
        discount — даёт скидку discount_percent% при покупке подписки

    plan_id = None  → скидка действует на любой тариф
    plan_id = int   → скидка только на конкретный тариф

    max_usages_per_client — сколько раз один клиент может использовать
                            (1 = одноразовый для каждого, 0 = без ограничений)
    """
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)
    promo_type: Mapped[PromocodeType] = mapped_column(
        Enum(PromocodeType), default=PromocodeType.BALANCE
    )

    # Для type=balance: сколько рублей начислить
    value: Mapped[float] = mapped_column(default=0.0)

    # Для type=discount: процент скидки и на какой план (None = все)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)
    plan_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("service_plans.id"), nullable=True
    )

    max_usages: Mapped[int] = mapped_column(default=1)          # всего активаций (0 = ∞)
    max_usages_per_client: Mapped[int] = mapped_column(default=1)  # на одного клиента (0 = ∞)
    current_usages: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    plan: Mapped[Optional["ServicePlan"]] = relationship()
    usages: Mapped[List["PromocodeUsage"]] = relationship(back_populates="promocode", cascade="all, delete-orphan")


class PromocodeUsage(Base):
    """
    Журнал использования промокодов.
    Нужен чтобы контролировать лимит на одного клиента.
    """
    __tablename__ = "promocode_usages"

    id: Mapped[int] = mapped_column(primary_key=True)
    promocode_id: Mapped[int] = mapped_column(ForeignKey("promocodes.id"))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    used_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    promocode: Mapped["Promocode"] = relationship(back_populates="usages")
    client: Mapped["Client"] = relationship()


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