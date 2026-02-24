from pydantic import BaseModel, EmailStr, Field

class Authorization(BaseModel):
    email: EmailStr
    password: str