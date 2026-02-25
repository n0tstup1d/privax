from fastapi import FastAPI, Depends, HTTPException
from dotenv import load_dotenv
from schemas import *
from database.database import get_db
from database.models import *
from sqlalchemy import select
from jose import jwt, JWTError
import os
from database.database import engine
from passlib.context import CryptContext
from auth.security import get_password_hash, verify_password, create_tokens
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.security import OAuth2PasswordBearer

app = FastAPI()

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

@app.on_event("startup")
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.post("/register")
async def register_client(body: Authorization, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).filter(Client.email == body.email))
    if result. scalar_one_or_none():
        raise HTTPException(status_code=401, detail="Почта занята")
    
    hashed_pass = await get_password_hash(body.password)
    
    new_client = Client(email = body.email, password = hashed_pass)
    db.add(new_client)
    await db.commit()
    return {
        "status": "success"
    }

@app.post("/login")
async def login_user(body: Authorization, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).filter(Client.email == body.email))
    client = result.scalar_one_or_none()
    
    if not client or not await verify_password(body.password, client.password):
        raise HTTPException(status_code=401, detail="Не правильный пароль")
    
    access_token, refresh_token = await create_tokens({"sub": str(client.id)})
    
    db_token = RefreshToken(token=refresh_token, client_id = client.id)
    db.add(db_token)
    await db.commit()
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }
    

@app.post("/refresh")
async def refresh_session(refresh_token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RefreshToken).filter(RefreshToken.token==refresh_token))
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
    
    new_access, new_refresh = await create_tokens({"sub": client_id})
    
    db_token.token = new_refresh
    
    await db.commit()
    
    return { 
            "access_token": new_access,
            "refresh_token": new_refresh,
            "token_type": "bearer"
            }


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
    try:
        # 1. Расшифровываем Access Token
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Token invalid")
    except JWTError:
        # Если 5 минут прошли, JWTError сработает автоматически
        raise HTTPException(status_code=401, detail="Token expired or invalid")

    # 2. Достаем юзера из базы, чтобы убедиться, что он существует
    result = await db.execute(select(Client).where(Client.id == int(user_id)))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
        
    return user

@app.get("/my-data")
async def get_private_data(current_user: Client = Depends(get_current_user)):
    # Сюда попадет только тот, у кого живой Access Token
    return {"message": f"Привет, {current_user.email}!", "your_id": current_user.id}
