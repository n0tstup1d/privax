from fastapi import APIRouter, Depends, HTTPException
from auth.deps import get_current_admin
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from schemas import VPNServerCreate, VPNServerUpdate, VPNServerResponse
from database.database import get_db
from database.models import VPNServer
from app.services.ssh_service import check_and_get_marzban_token
from app.services.crypto_service import encrypt, decrypt
from typing import List

router = APIRouter()


@router.post("/servers/add", dependencies=[Depends(get_current_admin)])
async def add_vpn_server(body: VPNServerCreate, db: AsyncSession = Depends(get_db)):

    # 1. Проверяем нет ли уже сервера с таким IP
    existing = await db.execute(select(VPNServer).where(VPNServer.ip_address == body.ip_address))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Этот сервер уже есть в базе")

    # 2. Проверяем что сервер живой и Marzban отвечает
    result = await check_and_get_marzban_token(
        ip=body.ip_address,
        ssh_port=body.ssh_port,
        marzban_port=body.marzban_port,
        username=body.mar_admin_user,
        password=body.mar_admin_pass
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    # 3. Шифруем чувствительные данные перед сохранением
    new_server = VPNServer(
        name=body.name,
        ip_address=body.ip_address,
        country_code=body.country_code,
        tier_level=body.tier_level,
        marzban_port=body.marzban_port,
        ssh_port=body.ssh_port,
        mar_admin_user=encrypt(body.mar_admin_user),
        mar_admin_pass=encrypt(body.mar_admin_pass),
    )

    db.add(new_server)
    await db.commit()
    await db.refresh(new_server)

    return {"status": "Сервер добавлен в сеть Privax", "server_id": new_server.id}


@router.get("/servers", dependencies=[Depends(get_current_admin)], response_model=List[VPNServerResponse])
async def get_servers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VPNServer).order_by(VPNServer.tier_level))
    servers = result.scalars().all()

    # Расшифровываем для отображения админу
    response = []
    for s in servers:
        response.append(VPNServerResponse(
            id=s.id,
            name=s.name,
            ip_address=s.ip_address,
            country_code=s.country_code,
            tier_level=s.tier_level,
            ssh_port=s.ssh_port,
            marzban_port=s.marzban_port,
            mar_admin_user=decrypt(s.mar_admin_user),
            current_users_count=s.current_users_count,
            is_active=s.is_active
        ))
    return response


@router.patch("/servers/{server_id}", dependencies=[Depends(get_current_admin)])
async def update_server(server_id: int, body: VPNServerUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VPNServer).where(VPNServer.id == server_id))
    server = result.scalar_one_or_none()

    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    update_data = body.model_dump(exclude_none=True)

    # Если меняются данные подключения — проверяем соединение с новыми данными
    connection_fields = {"ip_address", "ssh_port", "marzban_port", "mar_admin_user", "mar_admin_pass"}
    if connection_fields & update_data.keys():

        # Берём актуальные значения — новые если пришли, старые если нет
        check_ip           = update_data.get("ip_address",     server.ip_address)
        check_ssh_port     = update_data.get("ssh_port",       server.ssh_port)
        check_marzban_port = update_data.get("marzban_port",   server.marzban_port)
        check_user         = update_data.get("mar_admin_user", decrypt(server.mar_admin_user))
        check_pass         = update_data.get("mar_admin_pass", decrypt(server.mar_admin_pass))

        check = await check_and_get_marzban_token(
            ip=check_ip,
            ssh_port=check_ssh_port,
            marzban_port=check_marzban_port,
            username=check_user,
            password=check_pass,
            
        )
        if not check["success"]:
            raise HTTPException(status_code=400, detail=f"Проверка соединения не прошла: {check['error']}")

    # Шифруем credentials перед сохранением
    if "mar_admin_user" in update_data:
        update_data["mar_admin_user"] = encrypt(update_data["mar_admin_user"])
    if "mar_admin_pass" in update_data:
        update_data["mar_admin_pass"] = encrypt(update_data["mar_admin_pass"])

    for field, value in update_data.items():
        setattr(server, field, value)

    await db.commit()
    return {"status": "Сервер обновлён"}


@router.delete("/servers/{server_id}", dependencies=[Depends(get_current_admin)])
async def delete_server(server_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VPNServer).where(VPNServer.id == server_id))
    server = result.scalar_one_or_none()

    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    if server.current_users_count > 0:
        raise HTTPException(status_code=400, detail="Нельзя удалить сервер с активными пользователями")

    await db.delete(server)
    await db.commit()
    return {"status": "Сервер удалён"}