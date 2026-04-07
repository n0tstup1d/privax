from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import os
from database.models import Base
from database.database import engine
from app.routers import auth, users, subscriptions, billing, faq, support
from app.routers.admin import server, plans, domains, notifications
from app.routers.admin import admin_auth, admin_stats, admin_managers
from app.scheduler import start_scheduler
from fastapi.middleware.cors import CORSMiddleware
from app.routers import admin_clients
from app.routers import subscription_serve
from app.routers import promocodes
from app.routers import referrals
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Privax API")

os.makedirs("uploads/support", exist_ok=True)
os.makedirs("uploads/faq", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
ADMIN_URL    = os.getenv("ADMIN_URL",    "http://localhost:5174")
allowed_origins = list({FRONTEND_URL, ADMIN_URL})

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    start_scheduler()


app.include_router(auth.router,               prefix="/auth",          tags=["Authentication"])
app.include_router(users.router,              prefix="/users",         tags=["Users"])
app.include_router(subscriptions.router,      prefix="/subscriptions", tags=["Subscriptions"])
app.include_router(billing.router,            prefix="/billing",       tags=["Billing"])
app.include_router(server.router,             prefix="/server",        tags=["Servers(adm)"])
app.include_router(plans.router,                                       tags=["Plans(adm)"])
app.include_router(domains.router,            prefix="/admin",         tags=["Domains(adm)"])
app.include_router(admin_clients.router,      prefix="/admin",         tags=["Admin"])
app.include_router(subscription_serve.router,                          tags=["Subscriptions"])
app.include_router(promocodes.router,                                  tags=["Promocodes"])
app.include_router(referrals.router,          prefix="/referral",      tags=["Referral"])
app.include_router(notifications.router,                               tags=["Notifications(adm)"])
app.include_router(admin_auth.router)
app.include_router(admin_stats.router)
app.include_router(admin_managers.router)
app.include_router(faq.router,                                         tags=["FAQ"])
app.include_router(support.router,                                     tags=["Support"])


@app.get("/")
async def root():
    return {"message": "Privax API is running"}