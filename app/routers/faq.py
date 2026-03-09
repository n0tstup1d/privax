import os
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth.deps import get_current_admin
from database.database import get_db
from database.models import FaqArticle, FaqImage, Client

router = APIRouter()

UPLOAD_DIR = "uploads/faq"
MAX_SIZE   = 5 * 1024 * 1024  # 5 МБ
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ─── Схемы ──────────────────────────────────────────────────────────

class ArticleCreate(BaseModel):
    title:        str
    slug:         Optional[str] = None
    category:     str = "Общее"
    content:      str
    is_published: bool = False

class ArticleUpdate(BaseModel):
    title:        Optional[str]  = None
    slug:         Optional[str]  = None
    category:     Optional[str]  = None
    content:      Optional[str]  = None
    is_published: Optional[bool] = None

# ─── Хелперы ────────────────────────────────────────────────────────

def make_slug(title: str) -> str:
    import re
    slug = title.lower()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug.strip())
    return re.sub(r'-+', '-', slug) or "article"

def fmt_image(img: FaqImage) -> dict:
    return {
        "id":       img.id,
        "filename": img.filename,
        "mime_type": img.mime_type,
        "url":      f"/uploads/faq/{img.stored_name}",
    }

def fmt_article(a: FaqArticle) -> dict:
    return {
        "id":           a.id,
        "title":        a.title,
        "slug":         a.slug,
        "category":     a.category,
        "content":      a.content,
        "is_published": a.is_published,
        "created_at":   a.created_at.isoformat(),
        "updated_at":   a.updated_at.isoformat(),
        "images":       [fmt_image(img) for img in a.images],
    }

# ─── Публичные ──────────────────────────────────────────────────────

@router.get("/faq")
async def list_faq(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(FaqArticle)
        .where(FaqArticle.is_published == True)
        .options(selectinload(FaqArticle.images))
        .order_by(FaqArticle.category, FaqArticle.created_at)
    )
    return [fmt_article(a) for a in result.scalars().all()]

@router.get("/faq/{slug}")
async def get_faq_article(slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(FaqArticle)
        .where(FaqArticle.slug == slug, FaqArticle.is_published == True)
        .options(selectinload(FaqArticle.images))
    )
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(404, "Статья не найдена")
    return fmt_article(article)

# ─── Admin ──────────────────────────────────────────────────────────

@router.get("/admin/faq")
async def admin_list(db: AsyncSession = Depends(get_db), _: Client = Depends(get_current_admin)):
    result = await db.execute(
        select(FaqArticle).options(selectinload(FaqArticle.images)).order_by(FaqArticle.updated_at.desc())
    )
    return [fmt_article(a) for a in result.scalars().all()]

@router.post("/admin/faq")
async def admin_create(body: ArticleCreate, db: AsyncSession = Depends(get_db), _: Client = Depends(get_current_admin)):
    slug = body.slug or make_slug(body.title)
    if (await db.execute(select(FaqArticle).where(FaqArticle.slug == slug))).scalar_one_or_none():
        slug = f"{slug}-{int(datetime.utcnow().timestamp())}"
    article = FaqArticle(title=body.title, slug=slug, category=body.category, content=body.content, is_published=body.is_published)
    db.add(article)
    await db.commit()
    await db.refresh(article)
    result = await db.execute(select(FaqArticle).where(FaqArticle.id == article.id).options(selectinload(FaqArticle.images)))
    return fmt_article(result.scalar_one())

@router.put("/admin/faq/{article_id}")
async def admin_update(article_id: int, body: ArticleUpdate, db: AsyncSession = Depends(get_db), _: Client = Depends(get_current_admin)):
    article = await db.get(FaqArticle, article_id)
    if not article:
        raise HTTPException(404)
    if body.title        is not None: article.title        = body.title
    if body.slug         is not None: article.slug         = body.slug
    if body.category     is not None: article.category     = body.category
    if body.content      is not None: article.content      = body.content
    if body.is_published is not None: article.is_published = body.is_published
    article.updated_at = datetime.utcnow()
    await db.commit()
    result = await db.execute(select(FaqArticle).where(FaqArticle.id == article_id).options(selectinload(FaqArticle.images)))
    return fmt_article(result.scalar_one())

@router.delete("/admin/faq/{article_id}")
async def admin_delete(article_id: int, db: AsyncSession = Depends(get_db), _: Client = Depends(get_current_admin)):
    article = await db.get(FaqArticle, article_id)
    if not article:
        raise HTTPException(404)
    # Удаляем файлы с диска
    result = await db.execute(select(FaqImage).where(FaqImage.article_id == article_id))
    for img in result.scalars().all():
        path = os.path.join(UPLOAD_DIR, img.stored_name)
        if os.path.exists(path):
            os.remove(path)
    await db.delete(article)
    await db.commit()
    return {"status": "ok"}

@router.post("/admin/faq/{article_id}/images")
async def admin_upload_image(
    article_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: Client = Depends(get_current_admin),
):
    article = await db.get(FaqArticle, article_id)
    if not article:
        raise HTTPException(404)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Только изображения")
    raw = await file.read()
    if len(raw) > MAX_SIZE:
        raise HTTPException(400, "Файл больше 5 МБ")

    ext = os.path.splitext(file.filename or "img")[1].lower() or ".jpg"
    stored_name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(UPLOAD_DIR, stored_name), "wb") as fp:
        fp.write(raw)

    img = FaqImage(article_id=article_id, filename=file.filename or stored_name, stored_name=stored_name, mime_type=file.content_type)
    db.add(img)
    await db.commit()
    await db.refresh(img)
    return fmt_image(img)

@router.delete("/admin/faq/images/{image_id}")
async def admin_delete_image(image_id: int, db: AsyncSession = Depends(get_db), _: Client = Depends(get_current_admin)):
    img = await db.get(FaqImage, image_id)
    if not img:
        raise HTTPException(404)
    path = os.path.join(UPLOAD_DIR, img.stored_name)
    if os.path.exists(path):
        os.remove(path)
    await db.delete(img)
    await db.commit()
    return {"status": "ok"}