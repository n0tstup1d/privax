import random
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt, JWTError
from typing import Optional
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
import os
from dotenv import load_dotenv

from auth.deps import get_current_user
from auth.security import get_password_hash, verify_password, create_tokens
from database.database import get_db
from database.models import Client, RefreshToken, Config, LoginAttempt, PasswordResetCode, Referral
from schemas import Authorization

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM")

# Домен твоего сайта — для правильного scope cookies
# Поставь в .env: SITE_DOMAIN=privax.com
SITE_DOMAIN = os.getenv("SITE_DOMAIN", "localhost")
IS_PROD     = os.getenv("ENV", "dev") == "prod"

# --- Антибрутфорс ---
MAX_ATTEMPTS   = 5    # попыток
BRUTE_WINDOW   = 15   # минут
BLOCK_DURATION = 30   # минут

# --- Сброс пароля ---
RESET_TTL = 10  # минут


router = APIRouter()


# ═══════════════════════════════════════════════
#  ME — проверка живой сессии
# ═══════════════════════════════════════════════

@router.get("/me")
async def get_me(current_user: Client = Depends(get_current_user)):
    """
    Лёгкий эндпоинт для проверки валидности access_token.
    Фронт вызывает при загрузке страницы (PrivateRoute).
    200 — залогинен, 401 — нужен refresh.
    """
    return {"id": current_user.id, "email": current_user.email, "is_admin": current_user.is_admin}
# ═══════════════════════════════════════════════

def _set_auth_cookies(response: Response, access_token: str, refresh_token: str):
    """
    Кладёт токены в httpOnly cookies.

    httpOnly=True  — JS вообще не видит куку, XSS бесполезен
    secure=True    — только по HTTPS (в проде обязательно)
    samesite="lax" — защита от CSRF для большинства случаев
    """
    # access_token: короткий TTL, читается на каждый запрос
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=IS_PROD,          # True на проде (HTTPS), False на localhost
        samesite="lax",
        max_age=60 * 30,         # 30 минут
        domain=SITE_DOMAIN if IS_PROD else None,
        path="/",
    )
    # refresh_token: долгий TTL, используется для /auth/refresh
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=IS_PROD,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 дней
        domain=SITE_DOMAIN if IS_PROD else None,
        path="/",                    # path="/" — браузер шлёт на все запросы
    )


def _clear_auth_cookies(response: Response):
    """Удаляет обе куки — используется при logout."""
    response.delete_cookie("access_token",  path="/")
    response.delete_cookie("refresh_token", path="/")


# ═══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════

def _generate_code() -> str:
    return "".join(random.choices(string.digits, k=6))


def _serialize_config(c: Config) -> dict:
    now = datetime.utcnow()
    return {
        "id": c.id,
        "sub_url": f"/sub/{c.sub_token}" if c.sub_token else None,
        "expires_at": c.expire_at.isoformat() + "Z",
        "expired": c.expire_at < now,
        "auto_renew": c.auto_renew,
    }


async def _get_user_configs(client_id: int, db: AsyncSession) -> list:
    result = await db.execute(
        select(Config)
        .where(Config.client_id == client_id)
        .order_by(Config.expire_at.desc())
    )
    return result.scalars().all()


async def _check_brute_force(email: str, ip: str, db: AsyncSession):
    now = datetime.utcnow()

    blocked = await db.execute(
        select(LoginAttempt)
        .where(LoginAttempt.email == email, LoginAttempt.blocked_until > now)
        .order_by(LoginAttempt.blocked_until.desc())
        .limit(1)
    )
    block = blocked.scalar_one_or_none()
    if block:
        wait = int((block.blocked_until - now).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток. Попробуйте через {wait} мин."
        )

    window_start = now - timedelta(minutes=BRUTE_WINDOW)
    count = await db.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.email == email,
            LoginAttempt.attempted_at > window_start,
            LoginAttempt.blocked_until.is_(None)
        )
    )

    if count >= MAX_ATTEMPTS:
        db.add(LoginAttempt(
            email=email,
            ip_address=ip,
            blocked_until=now + timedelta(minutes=BLOCK_DURATION)
        ))
        await db.flush()
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток. Аккаунт заблокирован на {BLOCK_DURATION} мин."
        )


async def _record_failed_attempt(email: str, ip: str, db: AsyncSession):
    db.add(LoginAttempt(email=email, ip_address=ip))
    await db.flush()


async def _clear_attempts(email: str, db: AsyncSession):
    await db.execute(delete(LoginAttempt).where(LoginAttempt.email == email))


# ═══════════════════════════════════════════════
#  СХЕМЫ
# ═══════════════════════════════════════════════

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class ResetPasswordRequest(BaseModel):
    email: EmailStr

class ConfirmResetRequest(BaseModel):
    email: EmailStr
    code: str
    new_password: str


# ═══════════════════════════════════════════════
#  РЕГИСТРАЦИЯ
# ═══════════════════════════════════════════════

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    ref_code: Optional[str] = None


@router.post("/register")
async def register_client(
    body: RegisterRequest,
    response: Response,             # ← добавлен response для установки cookies
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Client).filter(Client.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Почта уже занята")

    new_client = Client(email=body.email, password=get_password_hash(body.password))
    db.add(new_client)
    await db.flush()

    if body.ref_code:
        referrer_q = await db.execute(
            select(Client).where(Client.referral_code == body.ref_code.upper())
        )
        referrer = referrer_q.scalar_one_or_none()
        if referrer and referrer.id != new_client.id:
            new_client.referred_by_id = referrer.id
            db.add(Referral(referrer_id=referrer.id, referred_id=new_client.id))

    access_token, refresh_token = create_tokens({"sub": str(new_client.id)})
    db.add(RefreshToken(token=refresh_token, client=new_client))
    await db.commit()

    # Кладём токены в httpOnly cookies — в тело НЕ возвращаем
    _set_auth_cookies(response, access_token, refresh_token)

    return {
        "status": "registered",
        "subscriptions": []
    }


# ═══════════════════════════════════════════════
#  ВХОД
# ═══════════════════════════════════════════════

@router.post("/login")
async def login_user(
    request: Request,
    response: Response,             # ← добавлен response
    body: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    ip = request.client.host
    await _check_brute_force(body.username, ip, db)

    result = await db.execute(select(Client).filter(Client.email == body.username))
    client = result.scalar_one_or_none()

    if not client or not verify_password(body.password, client.password):
        await _record_failed_attempt(body.username, ip, db)
        await db.commit()
        raise HTTPException(status_code=401, detail="Неправильный email или пароль")

    await _clear_attempts(body.username, db)

    access_token, refresh_token = create_tokens({"sub": str(client.id)})
    db.add(RefreshToken(token=refresh_token, client_id=client.id))
    await db.commit()

    # Кладём в cookies — токены в JSON не возвращаем
    _set_auth_cookies(response, access_token, refresh_token)

    configs = await _get_user_configs(client.id, db)
    serialized = [_serialize_config(c) for c in configs]

    return {
        "status": "ok",
        "subscriptions": {
            "active":  [c for c in serialized if not c["expired"]],
            "expired": [c for c in serialized if c["expired"]],
        }
    }


# ═══════════════════════════════════════════════
#  REFRESH / LOGOUT
# ═══════════════════════════════════════════════

@router.post("/refresh")
async def refresh_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db)
):
    # Читаем refresh_token из httpOnly cookie
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Сессия не найдена. Войдите заново.")

    result = await db.execute(
        select(RefreshToken).filter(RefreshToken.token == refresh_token)
    )
    db_token = result.scalar_one_or_none()
    if not db_token:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Сессия не найдена. Войдите заново.")

    try:
        payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        client_id = payload.get("sub")
    except JWTError:
        await db.delete(db_token)
        await db.commit()
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Срок сессии истёк")

    new_access, new_refresh = create_tokens({"sub": client_id})
    db_token.token = new_refresh
    await db.commit()

    # Обновляем обе cookies
    _set_auth_cookies(response, new_access, new_refresh)

    configs = await _get_user_configs(int(client_id), db)
    serialized = [_serialize_config(c) for c in configs]

    return {
        "status": "ok",
        "subscriptions": {
            "active":  [c for c in serialized if not c["expired"]],
            "expired": [c for c in serialized if c["expired"]],
        }
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db)
):
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        await db.execute(
            delete(RefreshToken).where(RefreshToken.token == refresh_token)
        )
        await db.commit()

    _clear_auth_cookies(response)
    return {"status": "Вы вышли из аккаунта"}


# ═══════════════════════════════════════════════
#  СМЕНА ПАРОЛЯ
# ═══════════════════════════════════════════════

@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    if not verify_password(body.old_password, current_user.password):
        raise HTTPException(status_code=400, detail="Старый пароль неверный")

    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 8 символов")

    current_user.password = get_password_hash(body.new_password)
    await db.execute(
        delete(RefreshToken).where(RefreshToken.client_id == current_user.id)
    )
    await db.commit()

    # Сбрасываем cookies — нужно войти заново
    _clear_auth_cookies(response)
    return {"status": "Пароль изменён. Войдите заново."}


# ═══════════════════════════════════════════════
#  СБРОС ПАРОЛЯ
# ═══════════════════════════════════════════════

@router.post("/reset-password")
async def reset_password_request(
    request: Request,
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db)
):
    ip = request.client.host
    await _check_brute_force(body.email, ip, db)

    result = await db.execute(select(Client).filter(Client.email == body.email))
    client = result.scalar_one_or_none()

    if client:
        await db.execute(
            delete(PasswordResetCode).where(
                PasswordResetCode.client_id == client.id,
                PasswordResetCode.is_used == False
            )
        )

        code = _generate_code()
        db.add(PasswordResetCode(
            client_id=client.id,
            code=code,
            expires_at=datetime.utcnow() + timedelta(minutes=RESET_TTL)
        ))
        await db.commit()

        # TODO: заменить на email когда подключишь SMTP
        print(f"\n{'='*40}")
        print(f"[RESET PASSWORD] Email: {client.email}")
        print(f"[RESET PASSWORD] Code:  {code}")
        print(f"[RESET PASSWORD] Expires in {RESET_TTL} minutes")
        print(f"{'='*40}\n")

    return {
        "status": "Если такой email существует — код сброса отправлен"
    }


@router.post("/reset-password/confirm")
async def reset_password_confirm(
    body: ConfirmResetRequest,
    db: AsyncSession = Depends(get_db)
):
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 8 символов")

    result = await db.execute(select(Client).filter(Client.email == body.email))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=400, detail="Неверный код или email")

    reset = await db.execute(
        select(PasswordResetCode).where(
            PasswordResetCode.client_id == client.id,
            PasswordResetCode.code == body.code,
            PasswordResetCode.is_used == False,
            PasswordResetCode.expires_at > datetime.utcnow()
        )
    )
    reset_code = reset.scalar_one_or_none()
    if not reset_code:
        raise HTTPException(status_code=400, detail="Неверный или истёкший код")

    reset_code.is_used = True
    client.password = get_password_hash(body.new_password)

    await db.execute(
        delete(RefreshToken).where(RefreshToken.client_id == client.id)
    )
    await db.commit()

    return {"status": "Пароль успешно изменён. Войдите с новым паролем."}