from pydantic import BaseModel, EmailStr, Field
from typing import Optional


# --- АВТОРИЗАЦИЯ ---

class Authorization(BaseModel):
    email: EmailStr
    password: str = Field(min_length=3, max_length=32)


# --- ТИРЫ СЕРВЕРОВ ---

class ServerTierCreate(BaseModel):
    level: int                          # уникальный номер: 1, 2, 3...
    name: str                           # внутреннее: "silver"
    display_name: str                   # публичное: "Silver"
    description: Optional[str] = None
    default_max_users: int = 100        # вместимость одного сервера этого тира
    max_sessions: int = 3               # макс. одновременных подписок у клиента
    speed_mbps: int = 0                 # 0 = не показываем клиенту
    priority: int = 1


class ServerTierUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    default_max_users: Optional[int] = None
    max_sessions: Optional[int] = None
    speed_mbps: Optional[int] = None
    priority: Optional[int] = None


class ServerTierResponse(BaseModel):
    id: int
    level: int
    name: str
    display_name: str
    description: Optional[str] = None
    default_max_users: int
    max_sessions: int
    speed_mbps: int
    priority: int

    class Config:
        from_attributes = True


# --- ТАРИФНЫЕ ПЛАНЫ ---

class ServicePlanCreate(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    tier_level: int                     # FK → ServerTier.level
    price: float                        # итоговая цена за весь период
    duration_days: int = 30             # длительность: 7, 30, 90, 180...
    discount_percent: float = 0.0
    purchase_limit: int = 0             # 0 = безлимит


class ServicePlanUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    duration_days: Optional[int] = None
    discount_percent: Optional[float] = None
    purchase_limit: Optional[int] = None
    is_hidden: Optional[bool] = None


class ServicePlanResponse(BaseModel):
    id: int
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    tier_level: int
    price: float
    duration_days: int
    is_hidden: bool
    discount_percent: float
    final_price: float

    @classmethod
    def from_orm_with_price(cls, plan):
        final = round(plan.price * (1 - plan.discount_percent / 100), 2)
        return cls(
            id=plan.id,
            name=plan.name,
            display_name=plan.display_name,
            description=plan.description,
            tier_level=plan.tier_level,
            price=plan.price,
            duration_days=plan.duration_days,
            is_hidden=plan.is_hidden,
            discount_percent=plan.discount_percent,
            final_price=final,
        )

    class Config:
        from_attributes = True


# --- СЕРВЕРЫ ---

class VPNServerCreate(BaseModel):
    """
    Данные для добавления нового Marzban-сервера.

    SSH-поля нужны для первичного harden:
      ssh_port     — порт SSH на сервере (обычно 22 для нового)
      ssh_user     — SSH-пользователь (обычно root)
      ssh_password — пароль для первого подключения.
                     После harden вход по паролю отключается навсегда.
                     Если не передать — попробуем подключиться по ключу
                     (для уже захардённых серверов).

    marzban_url  — полный URL Marzban-панели (https://host:8000).
                   Порт из этого URL закрывается снаружи через UFW при harden.
    """
    name: str
    ip_address: str
    tier_level: int
    country_code: str = "DE"

    # SSH для harden
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: Optional[str] = None     # None = уже захардён, подключаемся по ключу

    # Marzban API
    marzban_url: str                        # https://host:port
    mar_admin_user: str                     # логин администратора Marzban
    mar_admin_pass: str                     # пароль администратора Marzban

    # Reality настройки
    # Если не передать — ключи сгенерируются автоматически через xray x25519 на сервере
    sni_domain: str = "cdnjs.com"          # домен-маска (dest в realitySettings)
    reality_port: int = 443                 # порт подключения (обычно 443)
    private_key: Optional[str] = None      # если None — генерируется автоматически
    short_ids: Optional[list[str]] = None  # если None — генерируются автоматически (3 штуки)
    marzban_container: str = "marzban-marzban-1"  # имя Docker-контейнера


class VPNServerUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: Optional[str] = None
    country_code: Optional[str] = None
    tier_level: Optional[int] = None
    marzban_url: Optional[str] = None
    mar_admin_user: Optional[str] = None
    mar_admin_pass: Optional[str] = None
    is_active: Optional[bool] = None


class VPNServerResponse(BaseModel):
    id: int
    name: str
    ip_address: str
    country_code: str
    tier_level: int
    mar_admin_user: str
    current_users_count: int
    max_users: int
    marzban_url: str
    is_active: bool
    is_online: bool = True
    inbounds_json: Optional[str] = None
    reality_public_key: Optional[str] = None
    reality_short_ids: Optional[str] = None
    reality_sni: Optional[str] = None
    tier: Optional[ServerTierResponse] = None

    class Config:
        from_attributes = True


# --- ДОМЕНЫ ---

class DomainCreate(BaseModel):
    domain: str
    country_code: Optional[str] = None


class DomainUpdate(BaseModel):
    domain: Optional[str] = None
    country_code: Optional[str] = None
    is_active: Optional[bool] = None