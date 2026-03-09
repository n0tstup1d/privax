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
    months: int = 1
    discount_percent: float = 0.0


class ServicePlanUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    months: Optional[int] = None
    discount_percent: Optional[float] = None


class ServicePlanResponse(BaseModel):
    id: int
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    tier_level: int
    price: float
    months: int
    discount_percent: float
    final_price: float
    # max_sessions берётся из tier — не хранится в плане

    @classmethod
    def from_orm_with_price(cls, plan):
        base  = plan.price * plan.months
        final = round(base * (1 - plan.discount_percent / 100), 2)
        return cls(
            id=plan.id,
            name=plan.name,
            display_name=plan.display_name,
            description=plan.description,
            tier_level=plan.tier_level,
            price=plan.price,
            months=plan.months,
            discount_percent=plan.discount_percent,
            final_price=final,
        )

    class Config:
        from_attributes = True


# --- VPN СЕРВЕРЫ ---

class VPNServerCreate(BaseModel):
    name: str
    ip_address: str
    tier_level: int                 # FK → ServerTier.level
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str | None = None
    mar_admin_user: str
    mar_admin_pass: str
    country_code: str = "DE"
    marzban_port: int = 8000
    # max_users не нужен — берётся из ServerTier.default_max_users


class VPNServerUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: Optional[str] = None
    country_code: Optional[str] = None
    tier_level: Optional[int] = None
    ssh_port: Optional[int] = None
    marzban_port: Optional[int] = None
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
    max_users: int               # из ServerTier.default_max_users
    marzban_port: int
    is_active: bool
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