from pydantic import BaseModel, EmailStr, Field
from typing import Optional

class Authorization(BaseModel):
    email: EmailStr
    password: str = Field(min_length=3, max_length=32)

class VPNServerCreate(BaseModel):
    name: str
    ip_address: str
    tier_level: int
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str | None = None   # если захочешь по паролю а не по ключу
    mar_admin_user: str
    mar_admin_pass: str
    country_code: str = "DE"
    marzban_port: int = 8000

class ServicePlanCreate(BaseModel):
    name: str                        # Silver, Gold, Elite, Private
    tier_level: int                  # 1, 2, 3, 4
    price: float                     # Цена в рублях
    max_sessions: int                # Лимит устройств
    duration_days: int = 30          # Длительность подписки в днях


class ServicePlanUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    max_sessions: Optional[int] = None
    duration_days: Optional[int] = None


class ServicePlanResponse(BaseModel):
    id: int
    name: str
    tier_level: int
    price: float
    max_sessions: int
    duration_days: int

    class Config:
        from_attributes = True

# --- VPN СЕРВЕРЫ ---

class VPNServerUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: str
    country_code: Optional[str] = None
    tier_level: Optional[int] = None
    ssh_port: Optional[int] = None
    marzban_port: Optional[int] = None
    mar_admin_user: Optional[str] = None
    mar_admin_pass: Optional[str] = None
    is_active: Optional[bool] = None
    current_users_count: Optional[int] = None


class VPNServerResponse(BaseModel):
    id: int
    name: str
    ip_address: str
    country_code: str
    tier_level: int
    ssh_port: int
    marzban_port: int
    mar_admin_user: str       # расшифрованный, только для админа
    current_users_count: int
    is_active: bool

    class Config:
        from_attributes = True