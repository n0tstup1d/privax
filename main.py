from fastapi import FastAPI, Depends, HTTPException
from dotenv import load_dotenv
from schemas import *
from database.database import get_db
from database.models import *
from sqlalchemy import select
from jose import jwt
from database.database import engine
from passlib.context import CryptContext

from sqlalchemy.ext.asyncio import AsyncSession
app = FastAPI()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

load_dotenv()

@app.on_event("startup")
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.post("/register")
async def register_client(body: Authorization, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).filter(Client.email == body.email))
    if result. scalar_one_or_none():
        raise HTTPException(status_code=401, detail="Почта занята")
    
    new_client = Client(email = body.email, password = body.password)
    db.add(new_client)
    await db.commit()
    return {
        new_client
    }

@app.post("/login")
async def login_user(body: Authorization, db: AsyncSession = Depends(get_db)):
    client = await db.execute(select(Client).filter(Client.email == body.email, Client.password == body.password))
    return client.scalar_one_or_none()
    