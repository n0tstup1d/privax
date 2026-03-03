from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
import json

from database.database import get_db
from database.models import VPNServer, TrustedDomain
from schemas import VPNServerCreate, VPNServerUpdate, VPNServerResponse
from auth.deps import get_current_admin
from app.services.marzban_configurator import configure_server
from app.services.crypto_service import decrypt, encrypt

router = APIRouter()


async def _get_domains(db: AsyncSession, country_code: str) -> list[str]:
    result = await db.execute(
        select(TrustedDomain).where(
            TrustedDomain.is_active == True,
            or_(
                TrustedDomain.country_code == country_code,
                TrustedDomain.country_code.is_(None)  # NULL == None не работает в SQL
            )
        )
    )
    return [d.domain for d in result.scalars().all()]


@router.post("/servers/add", response_model=VPNServerResponse)
async def add_server(
    body: VPNServerCreate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    existing = await db.execute(
        select(VPNServer).where(VPNServer.ip_address == body.ip_address)
    )
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
        max_users=body.max_users,
        is_active=False,  # по умолчанию неактивен до успешной настройки
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
            current_users_count=server.max_users
        )

        if not config_result.get("success"):
            raise HTTPException(
                status_code=503,
                detail=f"Сервер добавлен в БД, но настройка не удалась: {config_result.get('error')}"
            )

        server.reality_public_key = config_result.get("public_key")
        server.reality_short_ids  = json.dumps(config_result.get("short_ids", []))
        server.server_names        = json.dumps(config_result.get("server_names", []))
        server.is_active = True

        await db.commit()
        await db.refresh(server)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Сервер добавлен в БД, но подключиться не удалось: {str(e)}"
        )

    return server


@router.get("/servers", response_model=list[VPNServerResponse])
async def list_servers(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(select(VPNServer).order_by(VPNServer.id))
    return result.scalars().all()


@router.get("/servers/{server_id}", response_model=VPNServerResponse)
async def get_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(select(VPNServer).where(VPNServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")
    return server


@router.patch("/servers/{server_id}", response_model=VPNServerResponse)
async def update_server(
    server_id: int,
    body: VPNServerUpdate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(select(VPNServer).where(VPNServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    data = body.model_dump(exclude_unset=True)

    # Поля которые нужно шифровать перед сохранением
    if "mar_admin_user" in data:
        data["mar_admin_user"] = encrypt(data["mar_admin_user"])
    if "mar_admin_pass" in data:
        data["mar_admin_pass"] = encrypt(data["mar_admin_pass"])

    for field, value in data.items():
        setattr(server, field, value)

    await db.commit()
    await db.refresh(server)
    return server


@router.post("/servers/{server_id}/reconfigure", response_model=VPNServerResponse)
async def reconfigure_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(select(VPNServer).where(VPNServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

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
            current_users_count=server.max_users
        )

        if not config_result.get("success"):
            raise HTTPException(status_code=503, detail=f"Перенастройка не удалась: {config_result.get('error')}")

        server.reality_public_key = config_result.get("public_key")
        server.reality_short_ids  = json.dumps(config_result.get("short_ids", []))
        server.server_names        = json.dumps(config_result.get("server_names", []))
        server.is_active = True

        await db.commit()
        await db.refresh(server)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Перенастройка не удалась: {str(e)}")

    return server


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