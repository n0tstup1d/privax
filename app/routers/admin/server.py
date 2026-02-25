from fastapi import APIRouter, Depends
from auth.deps import get_current_user
from database.models import Client

router = APIRouter()

