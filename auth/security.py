import bcrypt
from datetime import datetime, timedelta
from jose import jwt
import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")

def get_password_hash(password: str) -> str:
    # bcrypt требует байты, поэтому кодируем строку
    pwd_bytes = password.encode('utf-8')
    # Генерируем соль и хеш
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    # Возвращаем строку для хранения в БД
    return hashed.decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    # Кодируем введенный пароль и хеш из базы в байты для сравнения
    password_bytes = plain_password.encode('utf-8')
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hashed_bytes)

def create_tokens(data: dict):
    # Твоя функция создания токенов остается без изменений
    access_expire = datetime.utcnow() + timedelta(days=300)
    access_token = jwt.encode({**data, "exp": access_expire}, SECRET_KEY, algorithm=ALGORITHM)
    
    refresh_expire = datetime.utcnow() + timedelta(days=150)
    refresh_token = jwt.encode({**data, "exp": refresh_expire}, SECRET_KEY, algorithm=ALGORITHM)
    
    return access_token, refresh_token
