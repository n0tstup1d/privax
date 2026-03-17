from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from database.models import InboundType


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
    max_sessions: int = 3               # макс. одновременных устройств у клиента
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
    name: str                           # внутреннее имя "Silver 3 месяца"
    display_name: Optional[str] = None
    description: Optional[str] = None
    tier_level: int                     # FK → ServerTier.level
    price: float                        # базовая цена ЗА МЕСЯЦ
    duration_days: int = 30  # длительность в днях: 3, 7, 30, 90, 180...
    discount_percent: float = 0.0
    purchase_limit: int = 0             # 0 = безлимит, 1+ = максимум N покупок


class ServicePlanUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    duration_days: Optional[int] = None
    discount_percent: Optional[float] = None
    purchase_limit: Optional[int] = None


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
    # max_sessions берётся из tier — не хранится в плане

    @classmethod
    def from_orm_with_price(cls, plan):
        base  = plan.price * (plan.duration_days / 30)
        final = round(base * (1 - plan.discount_percent / 100), 2)
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


# --- VPN СЕРВЕРЫ ---

class VPNServerCreate(BaseModel):
    name: str
    ip_address: str
    tier_level: int                 # FK -> ServerTier.level
    ssh_port: int = 22
    ssh_user: str = "root"          # SSH-пользователь для первичной настройки
    ssh_password: str               # SSH-пароль для первичной настройки (после hardening вход по паролю отключается)
    mar_admin_user: str             # логин 3x-ui
    mar_admin_pass: str             # пароль 3x-ui
    country_code: str = "DE"
    panel_port: int = 2053          # порт 3x-ui панели
    panel_path: str = ""               # секретный путь панели (например /IowuXQyUA8bB)


class VPNServerUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: Optional[str] = None
    country_code: Optional[str] = None
    tier_level: Optional[int] = None
    ssh_port: Optional[int] = None
    panel_port: Optional[int] = None
    panel_path: Optional[str] = None
    mar_admin_user: Optional[str] = None
    mar_admin_pass: Optional[str] = None
    is_active: Optional[bool] = None


class VPNServerResponse(BaseModel):
    id: int
    name: str
    ip_address: str
    country_code: str
    tier_level: int
    ssh_port: int
    mar_admin_user: str
    current_users_count: int
    max_users: int
    panel_port: int
    panel_path: str = ""
    is_active: bool
    inbound_type: InboundType = InboundType.TCP_REALITY
    tier: Optional[ServerTierResponse] = None

    class Config:
        from_attributes = True


class DomainCreate(BaseModel):
    domain: str
    country_code: Optional[str] = None

class DomainUpdate(BaseModel):
    domain: Optional[str] = None 
    country_code: Optional[str] = None
    is_active: Optional[bool] = None