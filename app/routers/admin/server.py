from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
import json

from database.database import get_db
from database.models import VPNServer, TrustedDomain, ServerTier
from schemas import (
    VPNServerCreate, VPNServerUpdate, VPNServerResponse,
    ServerTierCreate, ServerTierUpdate, ServerTierResponse,
)
from auth.deps import get_current_admin
from app.services.marzban_configurator import configure_server
from app.services.crypto_service import decrypt, encrypt

router = APIRouter()


# ══════════════════════════════════════════════════════════════════
#  ТИРЫ
# ══════════════════════════════════════════════════════════════════

@router.post("/tiers", response_model=ServerTierResponse, dependencies=[Depends(get_current_admin)])
async def create_tier(body: ServerTierCreate, db: AsyncSession = Depends(get_db)):
    """Создать новый тир. level должен быть уникальным (1, 2, 3...)."""
    existing = await db.execute(select(ServerTier).where(ServerTier.level == body.level))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Тир с level={body.level} уже существует")

    tier = ServerTier(**body.model_dump())
    db.add(tier)
    await db.commit()
    await db.refresh(tier)
    return tier


@router.get("/tiers", response_model=list[ServerTierResponse], dependencies=[Depends(get_current_admin)])
async def list_tiers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServerTier).order_by(ServerTier.level))
    return result.scalars().all()


@router.get("/tiers/{level}", response_model=ServerTierResponse, dependencies=[Depends(get_current_admin)])
async def get_tier(level: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServerTier).where(ServerTier.level == level))
    tier = result.scalar_one_or_none()
    if not tier:
        raise HTTPException(status_code=404, detail="Тир не найден")
    return tier


@router.patch("/tiers/{level}", response_model=ServerTierResponse, dependencies=[Depends(get_current_admin)])
async def update_tier(level: int, body: ServerTierUpdate, db: AsyncSession = Depends(get_db)):
    """
    Обновить свойства тира. Изменение default_max_users или max_sessions
    сразу влияет на все серверы и планы этого уровня.
    """
    result = await db.execute(select(ServerTier).where(ServerTier.level == level))
    tier = result.scalar_one_or_none()
    if not tier:
        raise HTTPException(status_code=404, detail="Тир не найден")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(tier, field, value)

    await db.commit()
    await db.refresh(tier)
    return tier


@router.delete("/tiers/{level}", dependencies=[Depends(get_current_admin)])
async def delete_tier(level: int, db: AsyncSession = Depends(get_db)):
    """Нельзя удалить тир если к нему привязаны серверы или планы."""
    result = await db.execute(select(ServerTier).where(ServerTier.level == level))
    tier = result.scalar_one_or_none()
    if not tier:
        raise HTTPException(status_code=404, detail="Тир не найден")

    servers = await db.execute(select(VPNServer).where(VPNServer.tier_level == level))
    if servers.scalars().first():
        raise HTTPException(status_code=400, detail="Нельзя удалить тир: есть привязанные серверы")

    await db.delete(tier)
    await db.commit()
    return {"detail": f"Тир level={level} удалён"}


# ══════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════

async def _get_domains(db: AsyncSession, country_code: str) -> list[str]:
    result = await db.execute(
        select(TrustedDomain).where(
            TrustedDomain.is_active == True,
            or_(
                TrustedDomain.country_code == country_code,
                TrustedDomain.country_code.is_(None)
            )
        )
    )
    return [d.domain for d in result.scalars().all()]


def _server_to_response(server: VPNServer) -> VPNServerResponse:
    """Преобразует ORM-объект в VPNServerResponse, беря max_users из тира."""
    return VPNServerResponse(
        id=server.id,
        name=server.name,
        ip_address=server.ip_address,
        country_code=server.country_code,
        tier_level=server.tier_level,
        ssh_port=server.ssh_port,
        mar_admin_user=server.mar_admin_user,
        current_users_count=server.current_users_count,
        max_users=server.tier.default_max_users if server.tier else 0,
        marzban_port=server.marzban_port,
        is_active=server.is_active,
        tier=server.tier,
    )


async def _load_server(server_id: int, db: AsyncSession) -> VPNServer:
    result = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .where(VPNServer.id == server_id)
    )
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")
    return server


# ══════════════════════════════════════════════════════════════════
#  СЕРВЕРЫ
# ══════════════════════════════════════════════════════════════════

@router.post("/servers/add", response_model=VPNServerResponse)
async def add_server(
    body: VPNServerCreate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    # Проверяем что тир существует
    tier_result = await db.execute(select(ServerTier).where(ServerTier.level == body.tier_level))
    tier = tier_result.scalar_one_or_none()
    if not tier:
        raise HTTPException(
            status_code=400,
            detail=f"Тир level={body.tier_level} не найден. Создайте его через POST /server/tiers"
        )

    existing = await db.execute(select(VPNServer).where(VPNServer.ip_address == body.ip_address))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Сервер с таким IP уже существует")

    server = VPNServer(
        name=body.name,
        country_code=body.country_code,
        tier_level=body.tier_level,
        ip_address=body.ip_address,
        ssh_port=body.ssh_port,
        marzban_port=body.marzban_port,
        mar_admin_user=encrypt(body.mar_admin_user),
        mar_admin_pass=encrypt(body.mar_admin_pass),
        current_users_count=0,
        is_active=False,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)

    domains = await _get_domains(db, server.country_code)
    if not domains:
        raise HTTPException(
            status_code=400,
            detail="Сервер добавлен в БД со статусом неактивен. Сначала добавьте домены через /admin/domains, затем вызовите /reconfigure"
        )

    try:
        config_result = await configure_server(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            marzban_port=server.marzban_port,
            mar_admin_user=decrypt(server.mar_admin_user),
            mar_admin_pass=decrypt(server.mar_admin_pass),
            trusted_domains=domains,
            current_users_count=tier.default_max_users
        )

        if not config_result.get("success"):
            raise HTTPException(
                status_code=503,
                detail=f"Сервер добавлен в БД, но настройка не удалась: {config_result.get('error')}"
            )

        server.reality_public_key = config_result.get("public_key")
        server.reality_short_ids  = json.dumps(config_result.get("short_ids", []))
        server.server_names       = json.dumps(config_result.get("server_names", []))
        server.is_active = True

        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Сервер добавлен в БД, но подключиться не удалось: {str(e)}"
        )

    return _server_to_response(await _load_server(server.id, db))


@router.get("/servers", response_model=list[VPNServerResponse])
async def list_servers(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .order_by(VPNServer.id)
    )
    return [_server_to_response(s) for s in result.scalars().all()]


@router.get("/servers/{server_id}", response_model=VPNServerResponse)
async def get_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    return _server_to_response(await _load_server(server_id, db))


@router.patch("/servers/{server_id}", response_model=VPNServerResponse)
async def update_server(
    server_id: int,
    body: VPNServerUpdate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    server = await _load_server(server_id, db)
    data = body.model_dump(exclude_unset=True)

    # Если меняется tier_level — проверяем что новый тир существует
    if "tier_level" in data:
        tier_check = await db.execute(select(ServerTier).where(ServerTier.level == data["tier_level"]))
        if not tier_check.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"Тир level={data['tier_level']} не найден")

    if "mar_admin_user" in data:
        data["mar_admin_user"] = encrypt(data["mar_admin_user"])
    if "mar_admin_pass" in data:
        data["mar_admin_pass"] = encrypt(data["mar_admin_pass"])

    for field, value in data.items():
        setattr(server, field, value)

    await db.commit()
    return _server_to_response(await _load_server(server_id, db))


@router.post("/servers/{server_id}/reconfigure", response_model=VPNServerResponse)
async def reconfigure_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    server = await _load_server(server_id, db)

    domains = await _get_domains(db, server.country_code)
    if not domains:
        raise HTTPException(
            status_code=400,
            detail="Нет доменов-масок. Добавьте домены через /admin/domains"
        )

    try:
        config_result = await configure_server(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            marzban_port=server.marzban_port,
            mar_admin_user=decrypt(server.mar_admin_user),
            mar_admin_pass=decrypt(server.mar_admin_pass),
            trusted_domains=domains,
            current_users_count=server.tier.default_max_users
        )

        if not config_result.get("success"):
            raise HTTPException(status_code=503, detail=f"Перенастройка не удалась: {config_result.get('error')}")

        server.reality_public_key = config_result.get("public_key")
        server.reality_short_ids  = json.dumps(config_result.get("short_ids", []))
        server.server_names       = json.dumps(config_result.get("server_names", []))
        server.is_active = True

        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Перенастройка не удалась: {str(e)}")

    return _server_to_response(await _load_server(server_id, db))


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(select(VPNServer).where(VPNServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    await db.delete(server)
    await db.commit()
    return {"detail": "Сервер удалён"}