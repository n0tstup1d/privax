import json
import re
import os
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.database import get_db
from database.models import VPNServer, ServerTier, Config, ServicePlan
from schemas import (
    VPNServerCreate, VPNServerUpdate, VPNServerResponse,
    ServerTierCreate, ServerTierUpdate, ServerTierResponse,
)
from auth.deps import require_role, get_admin_from_jwt
from database.models import AdminRole
from app.services.marzban_configurator import check_and_get_inbounds, apply_xray_config
from app.services.marzban_service import (
    check_marzban_connection, check_marzban_via_ssh,
    create_marzban_user, delete_marzban_user, get_marzban_user,
)
from app.services.ssh_service import harden_server, check_ssh_connection, SSH_KEY_PATH, _get_public_key, generate_reality_keys
from app.services.crypto_service import decrypt, encrypt

router = APIRouter()


def _normalize_inbounds(inbounds_json_str: str) -> dict:
    """
    Нормализует inbounds из БД для передачи в Marzban API.
    Marzban ожидает {"vless": ["TAG"]}, а в БД может быть {"vless": [{"tag": "TAG", ...}]}
    """
    if not inbounds_json_str:
        return {"vless": ["VLESS TCP REALITY"]}
    raw = json.loads(inbounds_json_str) if isinstance(inbounds_json_str, str) else inbounds_json_str
    return {
        proto: [item["tag"] if isinstance(item, dict) else item for item in items]
        for proto, items in raw.items()
    }


def _pick_clean_vless(links: list, username: str) -> str | None:
    """
    Берёт первую VLESS-ссылку из списка и чистит название.
    🚀 Marz (username) [VLESS - tcp] → privax_username
    """
    from urllib.parse import quote
    for link in links:
        if isinstance(link, str) and link.startswith("vless://"):
            if "#" in link:
                base = link.split("#")[0]
                return f"{base}#{quote(username)}"
            return link
    return None


# ══════════════════════════════════════════════════════════════════
#  ТИРЫ
# ══════════════════════════════════════════════════════════════════

@router.post("/tiers", response_model=ServerTierResponse, dependencies=[Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))])
async def create_tier(body: ServerTierCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(ServerTier).where(ServerTier.level == body.level))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Тир с level={body.level} уже существует")
    tier = ServerTier(**body.model_dump())
    db.add(tier)
    await db.commit()
    await db.refresh(tier)
    return tier


@router.get("/tiers", response_model=list[ServerTierResponse], dependencies=[Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))])
async def list_tiers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServerTier).order_by(ServerTier.level))
    return result.scalars().all()


@router.get("/tiers/{level}", response_model=ServerTierResponse, dependencies=[Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))])
async def get_tier(level: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServerTier).where(ServerTier.level == level))
    tier = result.scalar_one_or_none()
    if not tier:
        raise HTTPException(status_code=404, detail="Тир не найден")
    return tier


@router.patch("/tiers/{level}", response_model=ServerTierResponse, dependencies=[Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))])
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


@router.delete("/tiers/{level}", dependencies=[Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))])
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

def _extract_port_from_url(url: str) -> int | None:
    """Извлекает порт из marzban_url. https://host:8000 → 8000."""
    match = re.search(r":(\d+)$", url.rstrip("/"))
    if match:
        return int(match.group(1))
    if url.startswith("https://"):
        return 443
    if url.startswith("http://"):
        return 80
    return None


def _server_to_response(server: VPNServer) -> VPNServerResponse:
    return VPNServerResponse(
        id=server.id,
        name=server.name,
        ip_address=server.ip_address,
        country_code=server.country_code,
        tier_level=server.tier_level,
        mar_admin_user=server.mar_admin_user,
        current_users_count=server.current_users_count,
        max_users=server.tier.default_max_users if server.tier else 0,
        marzban_url=server.marzban_url,
        is_active=server.is_active,
        is_online=server.is_online,
        inbounds_json=server.inbounds_json,
        reality_public_key=server.reality_public_key,
        reality_short_ids=server.reality_short_ids,
        reality_sni=server.reality_sni,
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
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """
    Добавляет Marzban-сервер. Полный флоу:

    1. Проверки (тир существует, IP не дубль)
    2. Проверка SSH-ключа на диске
    3. Проверка Marzban API (ДО harden — пока порт открыт)
    4. SSH harden — закрываем порты, оставляем SSH + 443
    5. Генерация X25519 ключей через SSH
    6. Применение Xray конфига через Marzban API
    7. Запись в БД
    """

    # ── 1. Предварительные проверки ──────────────────────────────

    tier_result = await db.execute(select(ServerTier).where(ServerTier.level == body.tier_level))
    tier = tier_result.scalar_one_or_none()
    if not tier:
        raise HTTPException(
            status_code=400,
            detail=f"Тир level={body.tier_level} не найден. Создайте через POST /server/tiers"
        )

    existing = await db.execute(select(VPNServer).where(VPNServer.ip_address == body.ip_address))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Сервер с таким IP уже существует")

    # ── 2. Проверка SSH-ключа на диске ───────────────────────────

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

    # ── 3. Проверка Marzban API (до harden — пока порт открыт) ──
    # Сначала пробуем напрямую. Если не получается — через SSH-туннель.

    check = await check_marzban_connection(
        marzban_url=body.marzban_url,
        admin_username=body.mar_admin_user,
        admin_password=body.mar_admin_pass,
    )

    if not check["success"]:
        # Прямое соединение не прошло — пробуем через SSH-туннель
        check_tunnel = await check_marzban_via_ssh(
            marzban_url=body.marzban_url,
            admin_username=body.mar_admin_user,
            admin_password=body.mar_admin_pass,
            ssh_ip=body.ip_address,
            ssh_port=body.ssh_port,
            ssh_user=body.ssh_user,
            ssh_key_path=SSH_KEY_PATH,
            ssh_password=body.ssh_password,
        )

        if check_tunnel["success"]:
            # Через туннель работает — значит порт закрыт снаружи (уже захардён)
            # Продолжаем нормально
            pass
        else:
            # Ни напрямую, ни через туннель не работает
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Сервер НЕ добавлен — Marzban API недоступен.\n"
                    f"Прямое подключение: {check['error']}\n"
                    f"Через SSH-туннель: {check_tunnel['error']}\n"
                    f"Проверьте: правильный ли URL панели, логин/пароль Marzban, "
                    f"запущен ли Marzban на сервере."
                )
            )

    # ── 4. SSH: harden или пропуск ───────────────────────────────

    # Сначала пробуем по ключу — вдруг сервер уже захардён
    key_check = await check_ssh_connection(
        ip=body.ip_address,
        ssh_port=body.ssh_port,
        ssh_user=body.ssh_user,
    )

    if key_check.get("success"):
        harden_summary = "Сервер уже настроен (SSH по ключу прошёл)"

    elif body.ssh_password:
        marzban_port = _extract_port_from_url(body.marzban_url)

        harden_result = await harden_server(
            ip=body.ip_address,
            ssh_port=body.ssh_port,
            marzban_port=marzban_port or 8000,
            ssh_user=body.ssh_user,
            ssh_password=body.ssh_password,
        )
        if not harden_result.get("success"):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Сервер НЕ добавлен — автозащита не прошла: {harden_result.get('error')}. "
                    "Исправьте проблему и повторите."
                )
            )
        harden_summary = harden_result.get("summary", "Harden выполнен")

    else:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Сервер НЕ добавлен — SSH по ключу не прошёл: {key_check.get('error')}. "
                "Если сервер новый — передайте ssh_password для первичной настройки."
            )
        )

    # ── 5. Генерация X25519 ключей (если не переданы вручную) ────

    private_key = body.private_key
    public_key  = None

    if not private_key:
        keys_result = await generate_reality_keys(
            ip=body.ip_address,
            ssh_port=body.ssh_port,
            ssh_user=body.ssh_user,
            container_name=body.marzban_container,
        )
        if not keys_result["success"]:
            raise HTTPException(
                status_code=503,
                detail=f"Сервер НЕ добавлен — не удалось сгенерировать ключи Reality: {keys_result['error']}"
            )
        private_key = keys_result["private_key"]
        public_key  = keys_result["public_key"]

    # shortIds — генерируем если не переданы

    short_ids = body.short_ids or [secrets.token_hex(8) for _ in range(3)]

    # ── 6. Применение Xray конфига в Marzban ─────────────────────

    config_result = await apply_xray_config(
        marzban_url=body.marzban_url,
        admin_username=body.mar_admin_user,
        admin_password=body.mar_admin_pass,
        private_key=private_key,
        short_ids=short_ids,
        sni_domain=body.sni_domain,
        port=body.reality_port,
    )
    if not config_result["success"]:
        raise HTTPException(
            status_code=503,
            detail=f"Сервер НЕ добавлен — не удалось применить Xray конфиг: {config_result['error']}"
        )

    inbounds_json = json.dumps(config_result.get("inbounds", {}))

    # ── 7. Запись в БД ────────────────────────────────────────────

    server = VPNServer(
        name=body.name,
        country_code=body.country_code,
        tier_level=body.tier_level,
        ip_address=body.ip_address,
        marzban_url=body.marzban_url,
        mar_admin_user=encrypt(body.mar_admin_user),
        mar_admin_pass=encrypt(body.mar_admin_pass),
        inbounds_json=inbounds_json,
        reality_public_key=public_key,
        reality_short_ids=json.dumps(short_ids),
        reality_sni=body.sni_domain,
        current_users_count=0,
        is_active=True,
        is_online=True,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)

    # ── 8. Создание тестового клиента ─────────────────────────────

    test_username = f"test_{body.name.lower().replace(' ', '_')}"
    test_result = await create_marzban_user(
        marzban_url=body.marzban_url,
        admin_username=body.mar_admin_user,
        admin_password=body.mar_admin_pass,
        username=test_username,
        expire_days=0,  # бессрочный
        inbounds=_normalize_inbounds(inbounds_json),
    )

    test_vless = None
    if test_result.get("success"):
        test_vless = _pick_clean_vless(test_result.get("links", []), test_username)

    resp = await _load_server(server.id, db)
    return {
        **_server_to_response(resp).model_dump(),
        "harden_summary": harden_summary,
        "inbounds": config_result.get("inbounds", {}),
        "test_client": {
            "username": test_username,
            "vless_link": test_vless,
            "success": test_result.get("success", False),
            "error": test_result.get("error"),
        },
    }


@router.post("/servers/{server_id}/harden")
async def harden_server_endpoint(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """Повторный запуск harden на сервере (если первый раз не прошёл)."""
    server = await _load_server(server_id, db)
    marzban_port = _extract_port_from_url(server.marzban_url)

    result = await harden_server(
        ip=server.ip_address,
        ssh_port=22,
        marzban_port=marzban_port or 8000,
    )
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error"))
    return {
        "detail": "Защита применена",
        "summary": result.get("summary"),
        "steps": result.get("details"),
    }


@router.get("/servers", response_model=list[VPNServerResponse])
async def list_servers(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
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
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    return _server_to_response(await _load_server(server_id, db))


@router.patch("/servers/{server_id}", response_model=VPNServerResponse)
async def update_server(
    server_id: int,
    body: VPNServerUpdate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
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

    for field, value in data.items():
        setattr(server, field, value)

    await db.commit()
    return _server_to_response(await _load_server(server_id, db))


@router.post("/servers/{server_id}/reconfigure")
async def reconfigure_server(
    server_id: int,
    sni_domain: str = "cdnjs.com",
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """
    Пересоздаёт Xray конфиг на сервере — генерирует новые ключи
    и применяет конфиг через Marzban API.
    Используй если конфиг слетел или нужно сменить ключи.
    """
    server = await _load_server(server_id, db)

    # Генерируем новые ключи через SSH
    keys_result = await generate_reality_keys(
        ip=server.ip_address,
        ssh_port=22,
        container_name="marzban-marzban-1",
    )
    if not keys_result["success"]:
        raise HTTPException(status_code=503, detail=f"Не удалось сгенерировать ключи: {keys_result['error']}")


    short_ids = [secrets.token_hex(8) for _ in range(3)]

    config_result = await apply_xray_config(
        marzban_url=server.marzban_url,
        admin_username=decrypt(server.mar_admin_user),
        admin_password=decrypt(server.mar_admin_pass),
        private_key=keys_result["private_key"],
        short_ids=short_ids,
        sni_domain=sni_domain,
    )
    if not config_result["success"]:
        raise HTTPException(status_code=503, detail=f"Не удалось применить конфиг: {config_result['error']}")

    server.reality_public_key = keys_result["public_key"]
    server.reality_short_ids  = json.dumps(short_ids)
    server.reality_sni        = sni_domain
    server.inbounds_json      = json.dumps(config_result.get("inbounds", {}))
    await db.commit()

    return {
        "status": "ok",
        "public_key": keys_result["public_key"],
        "short_ids": short_ids,
        "sni_domain": sni_domain,
    }



@router.post("/servers/{server_id}/refresh-inbounds", dependencies=[Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))])
async def refresh_server_inbounds(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """Перечитывает список inbounds с Marzban-панели и обновляет в БД."""
    server = await _load_server(server_id, db)

    inbounds_result = await check_and_get_inbounds(
        marzban_url=server.marzban_url,
        admin_username=decrypt(server.mar_admin_user),
        admin_password=decrypt(server.mar_admin_pass),
    )
    if not inbounds_result.get("success"):
        raise HTTPException(
            status_code=503,
            detail=f"Не удалось получить inbounds: {inbounds_result.get('error')}"
        )

    server.inbounds_json = json.dumps(inbounds_result.get("inbounds", {}))
    await db.commit()

    return _server_to_response(await _load_server(server_id, db))


@router.post("/servers/{server_id}/check")
async def check_server_api(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """Проверяет доступность Marzban API на сервере."""
    server = await _load_server(server_id, db)
    check = await check_marzban_connection(
        marzban_url=server.marzban_url,
        admin_username=decrypt(server.mar_admin_user),
        admin_password=decrypt(server.mar_admin_pass),
    )

    # Обновляем статус в БД чтобы клиентский Dashboard тоже видел
    from datetime import datetime
    server.is_online = check.get("success", False)
    server.last_checked_at = datetime.utcnow()
    await db.commit()

    return {"server_id": server_id, "name": server.name, **check}


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: int,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """
    Удаляет сервер. Если есть активные подписки — переносит их на другие серверы того же тира.
    
    Логика:
    1. Считаем активные подписки на этом сервере
    2. Если нет — просто удаляем
    3. Если есть — ищем серверы того же тира с свободными слотами
    4. Если мест хватает — переносим (удаляем из старого Marzban, создаём на новом)
    5. Если мест нет — отказываем (unless force=True)
    
    ?force=true — удалить сервер и деактивировать подписки без переноса
    """
    from app.services.marzban_service import create_marzban_user, delete_marzban_user
    from app.services.link_generator import generate_sub_token
    from uuid import uuid4

    server = await _load_server(server_id, db)

    # Активные подписки на этом сервере
    configs_result = await db.execute(
        select(Config)
        .options(selectinload(Config.plan).selectinload(ServicePlan.tier))
        .where(Config.server_id == server_id, Config.is_active == True)
    )
    active_configs = configs_result.scalars().all()

    # Удаляем тестового клиента из Marzban
    test_username = f"test_{server.name.lower().replace(' ', '_')}"
    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)
    await delete_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=test_username,
    )

    if not active_configs:
        # Нет активных подписок — просто удаляем
        await db.delete(server)
        await db.commit()
        return {"detail": "Сервер удалён", "migrated": 0}

    # Есть активные подписки — ищем куда переносить
    other_servers_result = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .where(
            VPNServer.tier_level == server.tier_level,
            VPNServer.id != server_id,
            VPNServer.is_active == True,
        )
        .order_by(VPNServer.current_users_count.asc())
    )
    other_servers = other_servers_result.scalars().all()

    # Считаем свободные слоты
    total_free = 0
    for s in other_servers:
        max_users = s.tier.default_max_users if s.tier else 0
        total_free += max(0, max_users - s.current_users_count)

    if total_free < len(active_configs) and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NOT_ENOUGH_SLOTS",
                "message": f"Нельзя удалить: {len(active_configs)} активных подписок, "
                           f"свободных слотов на других серверах: {total_free}",
                "active_subscriptions": len(active_configs),
                "available_slots": total_free,
            }
        )

    # Переносим подписки
    migrated = 0
    failed = []
    deactivated = 0

    for config in active_configs:
        # Ищем сервер с свободным слотом
        target_server = None
        for s in other_servers:
            max_users = s.tier.default_max_users if s.tier else 0
            if s.current_users_count < max_users:
                target_server = s
                break

        if not target_server:
            if force:
                # Деактивируем подписку
                config.is_active = False
                config.vless_link = None
                deactivated += 1
                continue
            else:
                break

        # Удаляем из старого Marzban
        await delete_marzban_user(
            marzban_url=server.marzban_url,
            admin_username=admin_user,
            admin_password=admin_pass,
            username=config.mar_username,
        )

        # Создаём на новом
        target_admin_user = decrypt(target_server.mar_admin_user)
        target_admin_pass = decrypt(target_server.mar_admin_pass)

        new_mar_username = f"privax_{config.client_id}_{uuid4().hex[:4]}"
        new_sub_token = generate_sub_token()
        inbounds = _normalize_inbounds(target_server.inbounds_json)

        create_result = await create_marzban_user(
            marzban_url=target_server.marzban_url,
            admin_username=target_admin_user,
            admin_password=target_admin_pass,
            username=new_mar_username,
            expire_days=0,
            expire_at=config.expire_at,
            inbounds=inbounds,
        )

        if create_result.get("success"):
            # Обновляем конфиг в БД
            links = create_result.get("links", [])
            vless_link = _pick_clean_vless(links, new_mar_username)

            config.server_id = target_server.id
            config.mar_username = new_mar_username
            config.sub_token = new_sub_token
            config.vless_link = vless_link
            config.subscription_url = create_result.get("subscription_url", "")

            target_server.current_users_count += 1
            server.current_users_count = max(0, server.current_users_count - 1)
            migrated += 1
        else:
            failed.append({
                "config_id": config.id,
                "error": create_result.get("error"),
            })
            if force:
                config.is_active = False
                config.vless_link = None
                deactivated += 1

    # Удаляем сервер
    await db.delete(server)
    await db.commit()

    result = {
        "detail": "Сервер удалён",
        "migrated": migrated,
    }
    if deactivated:
        result["deactivated"] = deactivated
    if failed:
        result["failed"] = failed

    return result


# ══════════════════════════════════════════════════════════════════
#  ТЕСТОВЫЙ КЛИЕНТ
# ══════════════════════════════════════════════════════════════════

@router.get("/servers/{server_id}/test-client")
async def get_test_client(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """Получает VLESS-ссылку тестового клиента с Marzban (живые данные)."""
    server = await _load_server(server_id, db)
    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    test_username = f"test_{server.name.lower().replace(' ', '_')}"

    user_data = await get_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=test_username,
    )

    if not user_data.get("success"):
        return {
            "exists": False,
            "username": test_username,
            "vless_link": None,
            "error": user_data.get("error"),
        }

    links = user_data.get("links", [])
    vless_link = _pick_clean_vless(links, test_username)

    return {
        "exists": True,
        "username": test_username,
        "vless_link": vless_link,
        "status": user_data.get("status"),
    }


@router.post("/servers/{server_id}/test-client")
async def create_test_client(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """
    Создаёт (или пересоздаёт) тестового клиента на сервере.
    Удаляет старого если существует, создаёт нового.
    """
    server = await _load_server(server_id, db)
    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    test_username = f"test_{server.name.lower().replace(' ', '_')}"

    # Удаляем старого если есть
    await delete_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=test_username,
    )

    # Создаём нового
    inbounds = _normalize_inbounds(server.inbounds_json)
    result = await create_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=test_username,
        expire_days=0,
        inbounds=inbounds,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=503,
            detail=f"Не удалось создать тестового клиента: {result.get('error')}"
        )

    links = result.get("links", [])
    vless_link = _pick_clean_vless(links, test_username)

    return {
        "username": test_username,
        "vless_link": vless_link,
        "status": "created",
    }


@router.delete("/servers/{server_id}/test-client")
async def delete_test_client(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role(AdminRole.OWNER, AdminRole.DEVELOPER))
):
    """Удаляет тестового клиента с сервера."""
    server = await _load_server(server_id, db)
    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    test_username = f"test_{server.name.lower().replace(' ', '_')}"

    result = await delete_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=test_username,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=503,
            detail=f"Не удалось удалить тестового клиента: {result.get('error')}"
        )

    return {"status": "deleted", "username": test_username}