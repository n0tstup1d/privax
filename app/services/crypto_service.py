from cryptography.fernet import Fernet
from dotenv import load_dotenv
import os

load_dotenv()

_key = os.getenv("ENCRYPTION_KEY")
if not _key:
    raise RuntimeError("ENCRYPTION_KEY не найден в .env")

_fernet = Fernet(_key.encode())


def encrypt(text: str) -> str:
    """Шифрует строку, возвращает зашифрованную строку"""
    return _fernet.encrypt(text.encode()).decode()


def decrypt(text: str) -> str:
    """Расшифровывает строку, возвращает оригинал"""
    return _fernet.decrypt(text.encode()).decode()