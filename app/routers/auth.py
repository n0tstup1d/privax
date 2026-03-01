from fastapi import APIRouter, Depends, HTTPException
from dotenv import load_dotenv
from schemas import Authorization
from database.database import get_db
from database.models import Client, RefreshToken, Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from jose import jwt, JWTError
from fastapi.security import OAuth2PasswordRequestForm
from auth.security import get_password_hash, verify_password, create_tokens
from datetime import datetime
import os

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")

router = APIRouter()


def _serialize_config(c: Config) -> dict:
    """
    Превращает объект Config в словарь для ответа клиенту.
    Вынесли в функцию чтобы использовать и в логине и в refresh.
    
    expired — флаг истечения. Даже если подписка истекла,
    мы всё равно её возвращаем — приложение покажет "продлить".
    """
    now = datetime.utcnow()
    is_expired = c.expire_at < now

    return {
        "id": c.id,
        "subscription_url": c.subscription_url,
        "expires_at": c.expire_at.isoformat(),
        "expired": is_expired,          # True = истекла, False = активна
        "auto_renew": c.auto_renew,
    }


async def _get_user_configs(client_id: int, db: AsyncSession) -> list:
    """
    Возвращает все конфиги пользователя — и активные и истёкшие.
    
    Почему возвращаем истёкшие тоже?
    Чтобы приложение могло показать "твоя подписка истекла, продли".
    Без этого клиент просто увидит пустой экран и не поймёт что произошло.
    
    Сортировка: сначала активные, потом истёкшие.
    """
    result = await db.execute(
        select(Config)
        .where(Config.client_id == client_id)
        .order_by(Config.expire_at.desc())  # свежие сверху
    )
    return result.scalars().all()


@router.post("/register")
async def register_client(body: Authorization, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).filter(Client.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=401, detail="Почта занята")

    hashed_pass = get_password_hash(body.password)
    new_client = Client(email=body.email, password=hashed_pass)
    db.add(new_client)
    await db.flush()

    access_token, refresh_token = create_tokens({"sub": str(new_client.id)})

    db_token = RefreshToken(token=refresh_token, client=new_client)
    db.add(db_token)
    await db.commit()

    # При регистрации подписок ещё нет — возвращаем пустой список
    return {
        "status": "registered",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "subscriptions": []             # приложение сразу покажет экран покупки
    }


@router.post("/login")
async def login_user(
    body: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Client).filter(Client.email == body.username))
    client = result.scalar_one_or_none()

    if not client or not verify_password(body.password, client.password):
        raise HTTPException(status_code=401, detail="Неправильный email или пароль")

    access_token, refresh_token = create_tokens({"sub": str(client.id)})

    db_token = RefreshToken(token=refresh_token, client_id=client.id)
    db.add(db_token)
    await db.commit()

    # Получаем все конфиги пользователя
    configs = await _get_user_configs(client.id, db)
    serialized = [_serialize_config(c) for c in configs]

    # Разделяем на активные и истёкшие для удобства приложения
    active = [c for c in serialized if not c["expired"]]
    expired = [c for c in serialized if c["expired"]]

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "subscriptions": {
            "active": active,       # эти сразу добавляем в AmneziaVPN
            "expired": expired,     # для этих показываем "продлить подписку"
        }
    }


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

    # При обновлении токена тоже возвращаем подписки —
    # приложение может вызывать refresh при запуске чтобы обновить состояние
    configs = await _get_user_configs(int(client_id), db)
    serialized = [_serialize_config(c) for c in configs]

    active = [c for c in serialized if not c["expired"]]
    expired = [c for c in serialized if c["expired"]]

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
        "subscriptions": {
            "active": active,
            "expired": expired,
        }
    }