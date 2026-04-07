import os
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, Request, Cookie
from fastapi.security import OAuth2PasswordBearer
from database.database import get_db
from database.models import Client, AdminProfile, AdminRole
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

SECRET_KEY       = os.getenv("SECRET_KEY")
ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET")
ALGORITHM        = os.getenv("ALGORITHM")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


# ─── Обычные пользователи ────────────────────────────────────

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    access_token_cookie: Optional[str] = Cookie(default=None, alias="access_token"),
    bearer_token: Optional[str] = Depends(oauth2_scheme),
):
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
    """Старая зависимость — оставлена для совместимости с существующими роутерами."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Доступ запрещён")
    return current_user


# ─── Админы — новая система ──────────────────────────────────

async def get_admin_from_jwt(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> tuple[Client, AdminProfile]:
    """
    Базовая зависимость для всех админских роутеров.
    Возвращает (client, admin_profile).
    Роль доступна через admin_profile.role
    """
    token = request.cookies.get("admin_token")
    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")

    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("scope") != "admin":
            raise HTTPException(status_code=403, detail="Нет доступа")
        client_id = int(payload["sub"])
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
    if not client:
        raise HTTPException(status_code=403, detail="Нет доступа")

    return client, profile


def require_role(*roles: AdminRole):
    """
    Фабрика зависимостей для проверки роли.

    Примеры использования:

        # В dependencies:
        @router.delete("/server/{id}", dependencies=[Depends(require_role(AdminRole.OWNER))])

        # В параметрах (если нужен доступ к client/profile):
        async def endpoint(admin=Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))):
            client, profile = admin
    """
    async def dependency(admin=Depends(get_admin_from_jwt)) -> tuple[Client, AdminProfile]:
        client, profile = admin
        if profile.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Недостаточно прав. Требуется: {', '.join(r.value for r in roles)}"
            )
        return client, profile
    return dependency


# ─── Готовые зависимости — используй в роутерах ──────────────

any_admin    = get_admin_from_jwt                                         # любой активный админ
owner_only   = require_role(AdminRole.OWNER)                              # только owner
owner_or_dev = require_role(AdminRole.OWNER, AdminRole.DEVELOPER)         # owner + developer
owner_or_op  = require_role(AdminRole.OWNER, AdminRole.OPERATOR)          # owner + operator
all_roles    = get_admin_from_jwt                                          # все роли (алиас)