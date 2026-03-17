from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
import json

from database.database import get_db
from database.models import VPNServer, TrustedDomain, ServerTier, InboundType
from schemas import (
    VPNServerCreate, VPNServerUpdate, VPNServerResponse,
    ServerTierCreate, ServerTierUpdate, ServerTierResponse,
)
from auth.deps import get_current_admin
from app.services.xui_configurator import ensure_tcp_reality_inbound
from app.services.ssh_service import harden_server, check_ssh_connection
from app.services.crypto_service import decrypt, encrypt

router = APIRouter()


# ══════════════════════════════════════════════════════════════════
#  ТИРЫ
# ══════════════════════════════════════════════════════════════════

@router.post("/tiers", response_model=ServerTierResponse, dependencies=[Depends(get_current_admin)])
async def create_tier(body: ServerTierCreate, db: AsyncSession = Depends(get_db)):
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
        panel_port=server.panel_port,
        panel_path=decrypt(server.panel_path) if server.panel_path else "",
        is_active=server.is_active,
        inbound_type=server.inbound_type,
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
    # ── Предварительные проверки (до любых SSH-соединений) ────────
    # 1. Тир существует?
    tier_result = await db.execute(select(ServerTier).where(ServerTier.level == body.tier_level))
    tier = tier_result.scalar_one_or_none()
    if not tier:
        raise HTTPException(
            status_code=400,
            detail=f"Тир level={body.tier_level} не найден. Создайте через POST /server/tiers"
        )

    # 2. IP уже в БД?
    existing = await db.execute(select(VPNServer).where(VPNServer.ip_address == body.ip_address))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Сервер с таким IP уже существует")

    # 3. Домены для страны есть?
    domains = await _get_domains(db, body.country_code)
    if not domains:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Нет доменов-масок для страны '{body.country_code}'. "
                "Добавьте через /admin/domains и повторите."
            )
        )

    # 4. SSH-ключ лежит на диске?
    from app.services.ssh_service import SSH_KEY_PATH, _get_public_key
    import os
    if not SSH_KEY_PATH or not os.path.exists(SSH_KEY_PATH):
        raise HTTPException(
            status_code=500,
            detail="SSH_KEY_PATH не задан или файл не найден. Проверьте .env"
        )
    if not _get_public_key():
        raise HTTPException(
            status_code=500,
            detail="Публичный SSH-ключ не найден (SSH_KEY_PATH_PUB). Проверьте .env"
        )

    # ── Шаг 1: SSH-подключение ────────────────────────────────────
    # Сначала пробуем по нашему ключу — вдруг сервер уже захардён.
    # Если не вышло И передан пароль — пробуем по паролю и запускаем harden.
    # Если ничего не сработало — стоп, в БД не пишем.
    key_check = await check_ssh_connection(
        ip=body.ip_address,
        ssh_port=body.ssh_port,
        ssh_user=body.ssh_user,
    )

    if key_check.get("success"):
        # Сервер уже захардён — harden пропускаем
        already_hardened = True
    elif body.ssh_password:
        # Пробуем по паролю и запускаем полный harden
        harden_result = await harden_server(
            ip=body.ip_address,
            ssh_port=body.ssh_port,
            panel_port=body.panel_port,
            ssh_user=body.ssh_user,
            ssh_password=body.ssh_password,
        )
        if not harden_result.get("success"):
            raise HTTPException(
                status_code=503,
                detail=f"Сервер НЕ добавлен в БД — автозащита не прошла: {harden_result.get('error')}."
            )
        already_hardened = False
    else:
        # Ни ключ не подошёл, ни пароля нет
        raise HTTPException(
            status_code=503,
            detail=(
                f"Сервер НЕ добавлен в БД — SSH по ключу не прошёл: {key_check.get('error')}. "
                "Если сервер новый — передайте ssh_password для первичной настройки."
            )
        )

    # ── Шаг 2: Запись в БД ───────────────────────────────────────
    server = VPNServer(
        name=body.name,
        country_code=body.country_code,
        tier_level=body.tier_level,
        ip_address=body.ip_address,
        ssh_port=body.ssh_port,
        panel_port=body.panel_port,
        mar_admin_user=encrypt(body.mar_admin_user),
        mar_admin_pass=encrypt(body.mar_admin_pass),
        panel_path=encrypt(body.panel_path) if body.panel_path else None,
        inbound_type=InboundType.TCP_REALITY,
        current_users_count=0,
        is_active=False,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)

    # ── Шаг 3: Создание/обновление инбаунда ──────────────────────
    # Всегда используем TCP Reality
    try:
        config_result = await ensure_tcp_reality_inbound(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            panel_port=server.panel_port,
            trusted_domains=domains,
            xui_admin_user=decrypt(server.mar_admin_user),
            xui_admin_pass=decrypt(server.mar_admin_pass),
            panel_path=decrypt(server.panel_path) if server.panel_path else "",
            inbound_port=443,
        )

        if not config_result.get("success"):
            await db.delete(server)
            await db.commit()
            raise HTTPException(
                status_code=503,
                detail=f"Сервер НЕ добавлен в БД — настройка инбаунда не удалась: {config_result.get('error')}"
            )

        server.reality_public_key = config_result.get("public_key")
        server.reality_short_ids  = json.dumps(config_result.get("short_ids", []))
        server.server_names       = json.dumps(config_result.get("server_names", []))
        server.is_active          = True
        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        await db.delete(server)
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail=f"Сервер НЕ добавлен в БД — ошибка настройки: {str(e)}"
        )

    resp = await _load_server(server.id, db)
    return {
        **_server_to_response(resp).__dict__,
        "inbound_action": config_result.get("action", "configured"),
    }


@router.post("/servers/{server_id}/harden")
async def harden_server_endpoint(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    """Повторный запуск автозащиты на сервере (если первый раз не прошёл)."""
    server = await _load_server(server_id, db)
    result = await harden_server(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        panel_port=server.panel_port,
    )
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error"))
    return {"detail": "Защита применена", "summary": result.get("summary"), "steps": result.get("details")}


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

    if "tier_level" in data:
        tier_check = await db.execute(select(ServerTier).where(ServerTier.level == data["tier_level"]))
        if not tier_check.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"Тир level={data['tier_level']} не найден")

    if "mar_admin_user" in data:
        data["mar_admin_user"] = encrypt(data["mar_admin_user"])
    if "mar_admin_pass" in data:
        data["mar_admin_pass"] = encrypt(data["mar_admin_pass"])
    if "panel_path" in data and data["panel_path"] is not None:
        data["panel_path"] = encrypt(data["panel_path"])

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
        raise HTTPException(status_code=400, detail="Нет доменов-масок. Добавьте через /admin/domains")

    # Всегда используем TCP Reality
    try:
        config_result = await ensure_tcp_reality_inbound(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            panel_port=server.panel_port,
            panel_path=decrypt(server.panel_path) if server.panel_path else "",
            trusted_domains=domains,
            xui_admin_user=decrypt(server.mar_admin_user),
            xui_admin_pass=decrypt(server.mar_admin_pass),
            inbound_port=443,
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