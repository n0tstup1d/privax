from fastapi import FastAPI
from database.models import Base
from database.database import engine
from app.routers import auth, users
from app.routers.admin import server

app = FastAPI(title="Privax API")


@app.on_event("startup")
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(server.router, prefix="/server", tags=["Servers(adm)"])

@app.get("/")
async def root():
    return {"message": "API is running"}


