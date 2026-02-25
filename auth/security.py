from passlib.context import CryptContext

from datetime import datetime, timedelta
from jose import jwt
from dotenv import load_dotenv
import os

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

async def get_password_hash(password):
    return pwd_context.hash(password)

async def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

async def create_tokens(data: dict):
    access_expire = datetime.utcnow()+timedelta(minutes=5)
    access_token = jwt.encode({**data, "exp": access_expire}, SECRET_KEY, algorithm=ALGORITHM)
    
    refresh_expire = datetime.utcnow() + timedelta(days=150)
    refresh_token = jwt.encode({**data, "exp": refresh_expire}, SECRET_KEY, algorithm=ALGORITHM)
    
    return access_token, refresh_token 

