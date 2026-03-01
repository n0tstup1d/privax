from fastapi import FastAPI
from database.models import Base
from database.database import engine
from app.routers import auth, users, subscriptions, billing
from app.routers.admin import server, plans
from app.scheduler import start_scheduler

app = FastAPI(title="Privax API")


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


@app.get("/")
async def root():
    return {"message": "Privax API is running"}