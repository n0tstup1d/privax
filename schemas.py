from pydantic import BaseModel, EmailStr, Field

class Authorization(BaseModel):
    email: EmailStr
    password: str = Field(min_length=3, max_length=32)

