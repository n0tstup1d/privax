from pydantic import BaseModel, EmailStr, Field
from typing import Optional


# --- АВТОРИЗАЦИЯ ---

class Authorization(BaseModel):
    email: EmailStr
    password: str = Field(min_length=3, max_length=32)


# --- ТАРИФНЫЕ ПЛАНЫ ---

class ServicePlanCreate(BaseModel):
    name: str                           # "Silver 3 месяца"
    tier_level: int                     # 1, 2, 3, 4
    price: float                        # базовая цена ЗА МЕСЯЦ
    months: int = 1                     # период: 1, 3, 6...
    discount_percent: float = 0.0       # скидка в %
    max_sessions: int = 3               # лимит устройств


class ServicePlanUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    months: Optional[int] = None
    discount_percent: Optional[float] = None
    max_sessions: Optional[int] = None


class ServicePlanResponse(BaseModel):
    id: int
    name: str
    tier_level: int
    price: float                        # базовая цена за месяц
    months: int
    discount_percent: float
    max_sessions: int
    final_price: float                  # итоговая цена — считаем в validator ниже

    @classmethod
    def from_orm_with_price(cls, plan):
        """
        Фабричный метод — создаёт объект из ORM модели и сразу считает final_price.
        Используем его в роутере вместо обычного from_attributes.
        """
        base = plan.price * plan.months
        final = round(base * (1 - plan.discount_percent / 100), 2)
        return cls(
            id=plan.id,
            name=plan.name,
            tier_level=plan.tier_level,
            price=plan.price,
            months=plan.months,
            discount_percent=plan.discount_percent,
            max_sessions=plan.max_sessions,
            final_price=final
        )

    class Config:
        from_attributes = True


# --- VPN СЕРВЕРЫ ---

class VPNServerCreate(BaseModel):
    name: str
    ip_address: str
    tier_level: int
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str | None = None
    mar_admin_user: str
    mar_admin_pass: str
    country_code: str = "DE"
    marzban_port: int = 8000


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
    marzban_port: int
    mar_admin_user: str
    current_users_count: int
    is_active: bool

    class Config:
        from_attributes = True