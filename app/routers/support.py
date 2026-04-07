import os
import uuid
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import get_current_user, require_role, get_admin_from_jwt
from database.database import get_db
from database.models import Client, SupportTicket, TicketReply, TicketAttachment, AdminRole

router = APIRouter()

TICKET_TYPES  = {'question', 'complaint', 'suggestion'}
UPLOAD_DIR    = "uploads/support"
MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_MIME  = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
    'application/pdf', 'text/plain',
    'application/zip', 'application/x-zip-compressed',
}

os.makedirs(UPLOAD_DIR, exist_ok=True)

_any_admin   = get_admin_from_jwt
_owner_or_op = require_role(AdminRole.OWNER, AdminRole.OPERATOR)


# ─── Хелперы ─────────────────────────────────

def fmt_attachment(a: TicketAttachment) -> dict:
    return {"id": a.id, "filename": a.filename, "mime_type": a.mime_type, "size": a.size, "url": f"/uploads/support/{a.stored_name}"}

def fmt_reply(r: TicketReply) -> dict:
    return {"id": r.id, "is_admin": r.is_admin, "message": r.message, "created_at": r.created_at.isoformat(), "attachments": [fmt_attachment(a) for a in r.attachments]}

def fmt_ticket(t: SupportTicket, with_client: bool = False) -> dict:
    d = {
        "id": t.id, "type": t.type, "subject": t.subject, "message": t.message,
        "status": t.status, "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat(),
        "attachments": [fmt_attachment(a) for a in t.attachments],
        "replies": [fmt_reply(r) for r in t.replies],
    }
    if with_client and t.client:
        d["client"] = {"id": t.client.id, "email": t.client.email}
    return d

async def save_files(files: List[UploadFile]) -> List[dict]:
    saved = []
    for f in files:
        if not f.filename: continue
        if f.content_type not in ALLOWED_MIME:
            raise HTTPException(400, f"Тип файла не разрешён: {f.content_type}")
        raw = await f.read()
        if len(raw) > MAX_FILE_SIZE:
            raise HTTPException(400, f"Файл {f.filename} превышает 10 МБ")
        ext = os.path.splitext(f.filename)[1].lower()
        stored_name = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_DIR, stored_name), "wb") as fp:
            fp.write(raw)
        saved.append({"filename": f.filename, "stored_name": stored_name, "mime_type": f.content_type, "size": len(raw)})
    return saved


# ─── Клиент ──────────────────────────────────

@router.get("/support/tickets")
async def my_tickets(db: AsyncSession = Depends(get_db), user: Client = Depends(get_current_user)):
    result = await db.execute(
        select(SupportTicket).where(SupportTicket.client_id == user.id)
        .options(selectinload(SupportTicket.attachments), selectinload(SupportTicket.replies).selectinload(TicketReply.attachments))
        .order_by(SupportTicket.updated_at.desc())
    )
    return [fmt_ticket(t) for t in result.scalars().all()]


@router.post("/support/tickets")
async def create_ticket(
    type: str = Form(...), subject: str = Form(...), message: str = Form(...),
    files: List[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db), user: Client = Depends(get_current_user),
):
    if type not in TICKET_TYPES: raise HTTPException(400, "Неверный тип обращения")
    if not subject.strip() or not message.strip(): raise HTTPException(400, "Заполните тему и сообщение")

    saved = await save_files([f for f in files if f.filename])
    ticket = SupportTicket(client_id=user.id, type=type, subject=subject.strip(), message=message.strip())
    db.add(ticket)
    await db.flush()
    for meta in saved:
        db.add(TicketAttachment(ticket_id=ticket.id, reply_id=None, **meta))
    await db.commit()

    result = await db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket.id)
        .options(selectinload(SupportTicket.attachments), selectinload(SupportTicket.replies).selectinload(TicketReply.attachments))
    )
    return fmt_ticket(result.scalar_one())


@router.post("/support/tickets/{ticket_id}/reply")
async def user_reply(
    ticket_id: int, message: str = Form(...),
    files: List[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db), user: Client = Depends(get_current_user),
):
    ticket = await db.get(SupportTicket, ticket_id)
    if not ticket or ticket.client_id != user.id: raise HTTPException(404, "Обращение не найдено")
    if ticket.status == "closed": raise HTTPException(400, "Обращение закрыто")

    saved = await save_files([f for f in files if f.filename])
    reply = TicketReply(ticket_id=ticket_id, is_admin=False, message=message.strip())
    db.add(reply)
    await db.flush()
    for meta in saved:
        db.add(TicketAttachment(ticket_id=None, reply_id=reply.id, **meta))
    ticket.status = "open"
    ticket.updated_at = datetime.utcnow()
    await db.commit()

    result = await db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id)
        .options(selectinload(SupportTicket.attachments), selectinload(SupportTicket.replies).selectinload(TicketReply.attachments))
    )
    return fmt_ticket(result.scalar_one())


@router.post("/support/tickets/{ticket_id}/close")
async def close_ticket(ticket_id: int, db: AsyncSession = Depends(get_db), user: Client = Depends(get_current_user)):
    ticket = await db.get(SupportTicket, ticket_id)
    if not ticket or ticket.client_id != user.id: raise HTTPException(404)
    ticket.status = "closed"
    ticket.updated_at = datetime.utcnow()
    await db.commit()
    return {"status": "ok"}


# ─── Админ ───────────────────────────────────

@router.get("/admin/tickets")
async def admin_list_tickets(
    db: AsyncSession = Depends(get_db),
    _=Depends(_any_admin),
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 30,
):
    """Список всех тикетов с фильтрацией по статусу."""
    query = select(SupportTicket).options(
        selectinload(SupportTicket.client),
        selectinload(SupportTicket.attachments),
        selectinload(SupportTicket.replies).selectinload(TicketReply.attachments),
    ).order_by(SupportTicket.updated_at.desc())

    if status: query = query.where(SupportTicket.status == status)

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    tickets = result.scalars().all()

    return {
        "total": total,
        "pages": -(-total // page_size),
        "items": [fmt_ticket(t, with_client=True) for t in tickets],
    }


@router.get("/admin/tickets/{ticket_id}")
async def admin_get_ticket(ticket_id: int, db: AsyncSession = Depends(get_db), _=Depends(_any_admin)):
    result = await db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id)
        .options(
            selectinload(SupportTicket.client),
            selectinload(SupportTicket.attachments),
            selectinload(SupportTicket.replies).selectinload(TicketReply.attachments),
        )
    )
    ticket = result.scalar_one_or_none()
    if not ticket: raise HTTPException(404, "Тикет не найден")
    return fmt_ticket(ticket, with_client=True)


class AdminReplyBody(BaseModel):
    message: str


@router.post("/admin/tickets/{ticket_id}/reply")
async def admin_reply(
    ticket_id: int,
    body: AdminReplyBody,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_op),
):
    """Ответ от имени поддержки."""
    ticket = await db.get(SupportTicket, ticket_id)
    if not ticket: raise HTTPException(404, "Тикет не найден")
    if ticket.status == "closed": raise HTTPException(400, "Тикет закрыт")

    reply = TicketReply(ticket_id=ticket_id, is_admin=True, message=body.message.strip())
    db.add(reply)
    ticket.status = "answered"
    ticket.updated_at = datetime.utcnow()
    await db.commit()

    result = await db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id)
        .options(
            selectinload(SupportTicket.client),
            selectinload(SupportTicket.attachments),
            selectinload(SupportTicket.replies).selectinload(TicketReply.attachments),
        )
    )
    return fmt_ticket(result.scalar_one(), with_client=True)


@router.patch("/admin/tickets/{ticket_id}/status")
async def admin_set_status(
    ticket_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_op),
    status: str = "closed",
):
    ticket = await db.get(SupportTicket, ticket_id)
    if not ticket: raise HTTPException(404, "Тикет не найден")
    if status not in ("open", "answered", "closed"): raise HTTPException(400, "Неверный статус")
    ticket.status = status
    ticket.updated_at = datetime.utcnow()
    await db.commit()
    return {"status": status}