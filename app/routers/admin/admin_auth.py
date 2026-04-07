import os
import secrets
import httpx
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import jwt, JWTError
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv

from auth.security import verify_password
from database.database import get_db
from database.models import Client, AdminProfile, AdminOTPCode

load_dotenv()

ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET")
ALGORITHM        = os.getenv("ALGORITHM", "HS256")
TG_BOT_TOKEN     = os.getenv("ADMIN_TG_BOT_TOKEN")
TG_CHAT_ID       = os.getenv("ADMIN_TG_CHAT_ID")
IS_PROD          = os.getenv("ENV", "dev") == "prod"

OTP_TTL_MINUTES     = 5
ADMIN_JWT_TTL_HOURS = 8

router = APIRouter(prefix="/admin/auth", tags=["Admin Auth"])


class AdminLoginRequest(BaseModel):
    email: str
    password: str

class AdminVerifyRequest(BaseModel):
    pending_token: str
    code: str


def _create_admin_jwt(client_id: int, role: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=ADMIN_JWT_TTL_HOURS)
    return jwt.encode(
        {"sub": str(client_id), "exp": expire, "scope": "admin", "role": role},
        ADMIN_JWT_SECRET,
        algorithm=ALGORITHM,
    )


def _set_admin_cookie(response: Response, token: str):
    response.set_cookie(
        key="admin_token", value=token,
        httponly=True, secure=IS_PROD, samesite="strict",
        max_age=60 * 60 * ADMIN_JWT_TTL_HOURS, path="/",
    )


def _clear_admin_cookie(response: Response):
    response.delete_cookie("admin_token", path="/")


async def _send_telegram_code(code: str, ip: str, role: str):
    role_labels = {"owner": "Владелец", "developer": "Разработчик", "operator": "Оператор"}
    text = (
        f"🔐 *Вход в админку Privax*\n\n"
        f"Роль: *{role_labels.get(role, role)}*\n"
        f"Код: `{code}`\n"
        f"IP: `{ip}`\n"
        f"Действителен: {OTP_TTL_MINUTES} минут\n\n"
        f"Если это не вы — немедленно смените пароль\\!"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "MarkdownV2",
        })
        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail="Ошибка отправки Telegram-кода")


@router.post("/login")
async def admin_login(
    body: AdminLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Шаг 1: проверяем email + пароль → шлём OTP в Telegram."""
    ip = request.client.host

    result = await db.execute(select(Client).where(Client.email == body.email))
    client = result.scalar_one_or_none()

    # Одинаковая ошибка — не раскрываем что именно неверно
    if not client or not verify_password(body.password, client.password):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    # Проверяем AdminProfile
    prof_result = await db.execute(
        select(AdminProfile).where(
            AdminProfile.client_id == client.id,
            AdminProfile.is_active == True,
        )
    )
    profile = prof_result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    # Удаляем старые OTP
    await db.execute(delete(AdminOTPCode).where(AdminOTPCode.client_id == client.id))

    pending_token = secrets.token_hex(32)
    otp_code      = str(secrets.randbelow(900000) + 100000)

    db.add(AdminOTPCode(
        client_id=client.id,
        pending_token=pending_token,
        code=otp_code,
        expires_at=datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES),
        ip_address=ip,
    ))
    await db.commit()

    await _send_telegram_code(otp_code, ip, profile.role.value)

    return {"status": "otp_sent", "pending_token": pending_token}


@router.post("/verify")
async def admin_verify(
    body: AdminVerifyRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Шаг 2: проверяем OTP → выдаём admin_token в cookie."""
    now = datetime.utcnow()

    result = await db.execute(
        select(AdminOTPCode).where(
            AdminOTPCode.pending_token == body.pending_token,
            AdminOTPCode.is_used == False,
            AdminOTPCode.expires_at > now,
        )
    )
    otp = result.scalar_one_or_none()
    if not otp:
        raise HTTPException(status_code=401, detail="Код недействителен или истёк")
    if otp.code != body.code.strip():
        raise HTTPException(status_code=401, detail="Неверный код")

    # Получаем роль
    prof_result = await db.execute(
        select(AdminProfile).where(AdminProfile.client_id == otp.client_id)
    )
    profile = prof_result.scalar_one_or_none()
    if not profile or not profile.is_active:
        raise HTTPException(status_code=403, detail="Нет доступа")

    # Обновляем last_login
    profile.last_login_at = now
    profile.last_login_ip = request.client.host
    otp.is_used = True
    await db.commit()

    token = _create_admin_jwt(otp.client_id, profile.role.value)
    _set_admin_cookie(response, token)

    return {"status": "ok", "role": profile.role.value}


@router.get("/me")
async def admin_me(request: Request, db: AsyncSession = Depends(get_db)):
    """Проверка живой сессии — возвращает email и роль."""
    token = request.cookies.get("admin_token")
    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")

    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("scope") != "admin":
            raise HTTPException(status_code=403, detail="Нет доступа")
        client_id = int(payload["sub"])
        role      = payload.get("role", "operator")
    except JWTError:
        raise HTTPException(status_code=401, detail="Сессия истекла")

    result = await db.execute(
        select(AdminProfile).where(
            AdminProfile.client_id == client_id,
            AdminProfile.is_active == True,
        )
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=403, detail="Нет доступа")

    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()

    return {"id": client.id, "email": client.email, "role": role}


@router.post("/logout")
async def admin_logout(response: Response):
    _clear_admin_cookie(response)
    return {"status": "ok"}