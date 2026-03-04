import random
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt, JWTError
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
import os
from dotenv import load_dotenv

from auth.deps import get_current_user
from auth.security import get_password_hash, verify_password, create_tokens
from database.database import get_db
from database.models import Client, RefreshToken, Config, LoginAttempt, PasswordResetCode
from schemas import Authorization

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM")

# --- Антибрутфорс ---
MAX_ATTEMPTS   = 5    # попыток
BRUTE_WINDOW   = 15   # минут — окно подсчёта
BLOCK_DURATION = 30   # минут — длина блокировки

# --- Сброс пароля ---
RESET_TTL = 10  # минут


router = APIRouter()


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
        "expires_at": c.expire_at.isoformat(),
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

@router.post("/register")
async def register_client(body: Authorization, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).filter(Client.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Почта уже занята")

    new_client = Client(email=body.email, password=get_password_hash(body.password))
    db.add(new_client)
    await db.flush()

    access_token, refresh_token = create_tokens({"sub": str(new_client.id)})
    db.add(RefreshToken(token=refresh_token, client=new_client))
    await db.commit()

    return {
        "status": "registered",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "subscriptions": []
    }


# ═══════════════════════════════════════════════
#  ВХОД
# ═══════════════════════════════════════════════

@router.post("/login")
async def login_user(
    request: Request,
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

    configs = await _get_user_configs(client.id, db)
    serialized = [_serialize_config(c) for c in configs]

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "subscriptions": {
            "active":  [c for c in serialized if not c["expired"]],
            "expired": [c for c in serialized if c["expired"]],
        }
    }


# ═══════════════════════════════════════════════
#  REFRESH / LOGOUT
# ═══════════════════════════════════════════════

@router.post("/refresh")
async def refresh_session(refresh_token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RefreshToken).filter(RefreshToken.token == refresh_token)
    )
    db_token = result.scalar_one_or_none()
    if not db_token:
        raise HTTPException(status_code=401, detail="Сессия не найдена. Войдите заново.")

    try:
        payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        client_id = payload.get("sub")
    except JWTError:
        await db.delete(db_token)
        await db.commit()
        raise HTTPException(status_code=401, detail="Срок сессии истёк")

    new_access, new_refresh = create_tokens({"sub": client_id})
    db_token.token = new_refresh
    await db.commit()

    configs = await _get_user_configs(int(client_id), db)
    serialized = [_serialize_config(c) for c in configs]

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
        "subscriptions": {
            "active":  [c for c in serialized if not c["expired"]],
            "expired": [c for c in serialized if c["expired"]],
        }
    }


@router.post("/logout")
async def logout(refresh_token: str, db: AsyncSession = Depends(get_db)):
    await db.execute(
        delete(RefreshToken).where(RefreshToken.token == refresh_token)
    )
    await db.commit()
    return {"status": "Вы вышли из аккаунта"}


# ═══════════════════════════════════════════════
#  СМЕНА ПАРОЛЯ (знает старый)
# ═══════════════════════════════════════════════

@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
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
    return {"status": "Пароль изменён. Войдите заново."}


# ═══════════════════════════════════════════════
#  СБРОС ПАРОЛЯ (забыл пароль)
# ═══════════════════════════════════════════════

@router.post("/reset-password")
async def reset_password_request(
    request: Request,
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Шаг 1: запрашиваем сброс.

    Сейчас код печатается в консоль сервера.
    Когда подключишь email — в этом файле найди строку
    '# TODO: заменить на email' и замени print() на send_email().

    Всегда возвращаем одинаковый ответ — чтобы нельзя было
    перебором определить существующие email.
    """
    ip = request.client.host
    await _check_brute_force(body.email, ip, db)

    result = await db.execute(select(Client).filter(Client.email == body.email))
    client = result.scalar_one_or_none()

    if client:
        # Удаляем старые неиспользованные коды
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
        # await send_password_reset(client.email, code)
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
    """Шаг 2: подтверждаем код и устанавливаем новый пароль."""
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

    # Инвалидируем все сессии
    await db.execute(
        delete(RefreshToken).where(RefreshToken.client_id == client.id)
    )
    await db.commit()

    return {"status": "Пароль успешно изменён. Войдите с новым паролем."}