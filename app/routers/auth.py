from fastapi import APIRouter, Depends, HTTPException
from dotenv import load_dotenv
from schemas import *
from database.database import get_db
from database.models import *
from sqlalchemy import select
from jose import jwt, JWTError
import os
from auth.security import get_password_hash, verify_password, create_tokens
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


@router.post("/register")
async def register_client(body: Authorization, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).filter(Client.email == body.email))
    if result. scalar_one_or_none():
        raise HTTPException(status_code=401, detail="Почта занята")
    print(body.password)
    print(body.password)
    print(body.password)
    hashed_pass = get_password_hash(body.password)
    
    new_client = Client(email = body.email, password = hashed_pass)
    db.add(new_client)
    await db.flush()
    
    access_token, refresh_token = create_tokens({"sub": str(new_client.id)})
    
    print(refresh_token)
    print(new_client.id)
    db_token = RefreshToken(token=refresh_token, client=new_client)
    db.add(db_token)
    await db.commit()
    return {
        "status": "registered and logged in",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }

@router.post("/login")
async def login_user(body: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).filter(Client.email == body.username))
    client = result.scalar_one_or_none()
    
    if not client or not verify_password(body.password, client.password):
        raise HTTPException(status_code=401, detail="Не правильный пароль")
    
    access_token, refresh_token = create_tokens({"sub": str(client.id)})
    
    db_token = RefreshToken(token=refresh_token, client_id = client.id)
    db.add(db_token)
    await db.commit()
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }
    

@router.post("/refresh")
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
    
    new_access, new_refresh = create_tokens({"sub": client_id})
    
    db_token.token = new_refresh
    
    await db.commit()
    
    return { 
            "access_token": new_access,
            "refresh_token": new_refresh,
            "token_type": "bearer"
            }