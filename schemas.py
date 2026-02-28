from pydantic import BaseModel, EmailStr, Field

class Authorization(BaseModel):
    email: EmailStr
    password: str = Field(min_length=3, max_length=32)

class VPNServerCreate(BaseModel):
    name: str
    ip_address: str
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str | None = None   # если захочешь по паролю а не по ключу
    mar_admin_user: str
    mar_admin_pass: str
    country_code: str = "DE"
    marzban_port: int = 8000