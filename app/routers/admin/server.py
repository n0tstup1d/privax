from fastapi import APIRouter, Depends, HTTPException
from auth.deps import get_current_admin
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from schemas import VPNServerCreate
from database.database import get_db
from database.models import VPNServer
from app.services.ssh_service import check_and_get_marzban_token
from app.services.crypto_service import encrypt

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
        ssh_port=body.ssh_port,        # добавить
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
        marzban_port=body.marzban_port,
        ssh_port=body.ssh_port,
        mar_admin_user=encrypt(body.mar_admin_user),
        mar_admin_pass=encrypt(body.mar_admin_pass),
    )

    db.add(new_server)
    await db.commit()

    return {"status": "Сервер добавлен в сеть Privax"}