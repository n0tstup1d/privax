from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from auth.deps import get_admin_from_jwt
from database.database import get_db
from database.models import (
    Client, Config, VPNServer, Invoice,
    InvoiceStatus, ServicePlan, SupportTicket,
)

router = APIRouter(prefix="/admin/stats", tags=["Admin Stats"])


@router.get("/dashboard")
async def get_dashboard(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_from_jwt),
):
    """
    Все данные для главной страницы админки.
    Один запрос — всё что нужно дашборду.
    """
    now = datetime.utcnow()
    today_start    = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago       = now - timedelta(days=7)
    month_ago      = now - timedelta(days=30)

    # ── Пользователи ─────────────────────────────────────────
    total_users = await db.scalar(select(func.count(Client.id)))

    new_users_today = await db.scalar(
        select(func.count(Client.id))
        # У Client нет created_at — считаем по invoice с самой ранней датой
        # Проще — считаем по ID (приблизительно)
    )
    # Новые за 7 дней — по инвойсам (у кого первый инвойс за неделю)
    new_users_week = await db.scalar(
        select(func.count(func.distinct(Invoice.client_id)))
        .where(Invoice.created_at >= week_ago)
    )

    banned_users = await db.scalar(
        select(func.count(Client.id)).where(Client.is_banned == True)
    )

    # ── Подписки ─────────────────────────────────────────────
    active_subs = await db.scalar(
        select(func.count(Config.id)).where(Config.is_active == True)
    )

    expired_subs = await db.scalar(
        select(func.count(Config.id)).where(
            Config.is_active == False,
            Config.expire_at < now,
        )
    )

    expiring_24h = await db.scalar(
        select(func.count(Config.id)).where(
            Config.is_active == True,
            Config.expire_at >= now,
            Config.expire_at < now + timedelta(hours=24),
        )
    )

    auto_renew_count = await db.scalar(
        select(func.count(Config.id)).where(
            Config.is_active == True,
            Config.auto_renew == True,
        )
    )

    # ── Доходы ───────────────────────────────────────────────
    revenue_today = await db.scalar(
        select(func.coalesce(func.sum(Invoice.amount), 0))
        .where(
            Invoice.status == InvoiceStatus.PAID,
            Invoice.created_at >= today_start,
        )
    )

    revenue_week = await db.scalar(
        select(func.coalesce(func.sum(Invoice.amount), 0))
        .where(
            Invoice.status == InvoiceStatus.PAID,
            Invoice.created_at >= week_ago,
        )
    )

    revenue_month = await db.scalar(
        select(func.coalesce(func.sum(Invoice.amount), 0))
        .where(
            Invoice.status == InvoiceStatus.PAID,
            Invoice.created_at >= month_ago,
        )
    )

    revenue_total = await db.scalar(
        select(func.coalesce(func.sum(Invoice.amount), 0))
        .where(Invoice.status == InvoiceStatus.PAID)
    )

    # Pending оплаты
    pending_invoices = await db.scalar(
        select(func.count(Invoice.id)).where(Invoice.status == InvoiceStatus.PENDING)
    )

    # ── Серверы ──────────────────────────────────────────────
    total_servers = await db.scalar(select(func.count(VPNServer.id)))
    online_servers = await db.scalar(
        select(func.count(VPNServer.id)).where(
            VPNServer.is_active == True,
            VPNServer.is_online == True,
        )
    )
    offline_servers = await db.scalar(
        select(func.count(VPNServer.id)).where(
            VPNServer.is_active == True,
            VPNServer.is_online == False,
        )
    )

    total_vpn_users = await db.scalar(
        select(func.coalesce(func.sum(VPNServer.current_users_count), 0))
    )

    # ── Тикеты поддержки ────────────────────────────────────
    open_tickets = await db.scalar(
        select(func.count(SupportTicket.id)).where(SupportTicket.status == "open")
    )

    # ── График доходов за 30 дней ────────────────────────────
    daily_revenue_rows = await db.execute(
        select(
            func.date_trunc("day", Invoice.created_at).label("day"),
            func.sum(Invoice.amount).label("amount"),
        )
        .where(
            Invoice.status == InvoiceStatus.PAID,
            Invoice.created_at >= month_ago,
        )
        .group_by("day")
        .order_by("day")
    )
    daily_revenue = [
        {"date": row.day.strftime("%Y-%m-%d"), "amount": float(row.amount)}
        for row in daily_revenue_rows
    ]

    # ── Популярные планы ─────────────────────────────────────
    plan_stats_rows = await db.execute(
        select(
            ServicePlan.display_name,
            ServicePlan.name,
            func.count(Config.id).label("count"),
        )
        .join(Config, Config.plan_id == ServicePlan.id)
        .where(Config.is_active == True)
        .group_by(ServicePlan.id, ServicePlan.display_name, ServicePlan.name)
        .order_by(func.count(Config.id).desc())
        .limit(5)
    )
    plan_stats = [
        {"name": row.display_name or row.name, "count": row.count}
        for row in plan_stats_rows
    ]

    # ── Серверы с нагрузкой ──────────────────────────────────
    server_rows = await db.execute(
        select(VPNServer).where(VPNServer.is_active == True)
        .order_by(VPNServer.current_users_count.desc())
    )
    servers_list = [
        {
            "id": s.id,
            "name": s.name,
            "country_code": s.country_code,
            "is_online": s.is_online,
            "current_users": s.current_users_count,
            "last_checked": s.last_checked_at.isoformat() + "Z" if s.last_checked_at else None,
        }
        for s in server_rows.scalars().all()
    ]

    return {
        "users": {
            "total": total_users,
            "new_week": new_users_week,
            "banned": banned_users,
        },
        "subscriptions": {
            "active": active_subs,
            "expired": expired_subs,
            "expiring_24h": expiring_24h,
            "auto_renew": auto_renew_count,
        },
        "revenue": {
            "today": float(revenue_today),
            "week": float(revenue_week),
            "month": float(revenue_month),
            "total": float(revenue_total),
            "pending_invoices": pending_invoices,
        },
        "servers": {
            "total": total_servers,
            "online": online_servers,
            "offline": offline_servers,
            "total_vpn_users": int(total_vpn_users),
            "list": servers_list,
        },
        "support": {
            "open_tickets": open_tickets,
        },
        "charts": {
            "daily_revenue": daily_revenue,
            "plan_stats": plan_stats,
        },
    }