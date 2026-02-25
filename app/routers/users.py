from fastapi import APIRouter, Depends
from auth.deps import get_current_user
from database.models import Client

router = APIRouter()

@router.get("/me")
async def get_my_profile(current_user: Client = Depends(get_current_user)):
    return {
        "email": current_user.email,
        "id": current_user.id,
        "info": "Это твои защищенные данные"
    }
