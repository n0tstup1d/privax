from fastapi import FastAPI
from database.models import Base
from database.database import engine
from app.routers import auth, users, subscriptions, billing
from app.routers.admin import server, plans, domains  
from app.scheduler import start_scheduler
from fastapi.middleware.cors import CORSMiddleware
from app.routers import admin_clients
from app.routers import subscription_serve
import os
from dotenv import load_dotenv

app = FastAPI(title="Privax API")

load_dotenv()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:5173")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def on_startup():
    # Создаём таблицы если их нет
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Запускаем фоновую задачу проверки истёкших подписок
    start_scheduler()


app.include_router(auth.router,              prefix="/auth",          tags=["Authentication"])
app.include_router(users.router,             prefix="/users",         tags=["Users"])
app.include_router(subscriptions.router,     prefix="/subscriptions", tags=["Subscriptions"])
app.include_router(billing.router,           prefix="/billing",       tags=["Billing"])
app.include_router(server.router,            prefix="/server",        tags=["Servers(adm)"])
app.include_router(plans.router,             prefix="/plans",         tags=["Plans(adm)"])
app.include_router(domains.router, prefix="/admin", tags=["Domains(adm)"])
app.include_router(admin_clients.router, prefix="/admin", tags=["Admin"])
app.include_router(subscription_serve.router, tags=["Subscriptions"])

@app.get("/")
async def root():
    return {"message": "Privax API is running"}