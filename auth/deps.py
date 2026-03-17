from jose import JWTError, jwt
from fastapi import Depends, HTTPException, Request, Cookie
from fastapi.security import OAuth2PasswordBearer
from database.database import get_db
from database.models import Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv
from typing import Optional
import os

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM")

# Для Swagger UI — показывает кнопку Authorize и принимает Bearer токен
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    access_token_cookie: Optional[str] = Cookie(default=None, alias="access_token"),
    bearer_token: Optional[str] = Depends(oauth2_scheme),
):
    """
    Поддерживает два способа авторизации:
    1. httpOnly cookie 'access_token' — браузер/фронт (безопасно, XSS не украдёт)
    2. Bearer токен в заголовке   — Swagger UI / прямые API запросы

    Cookie приоритетнее — если есть обе, используем cookie.
    """
    token = access_token_cookie or bearer_token

    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Token invalid")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token expired or invalid")

    result = await db.execute(select(Client).where(Client.id == int(user_id)))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if user.is_banned and not user.is_admin:
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    return user


async def get_current_admin(current_user: Client = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещён: требуются права администратора"
        )
    return current_user