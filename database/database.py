from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from dotenv import load_dotenv 
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL").replace(
    "postgresql://", "postgresql+asyncpg://"
)

engine = create_async_engine(DATABASE_URL, echo=True,  pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)

async def get_db():
    async with async_session() as session:
        yield session