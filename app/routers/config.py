from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Club, Admin

router = APIRouter(prefix="/config", tags=["config"])

class ClubUpdate(BaseModel):
    nombre: Optional[str] = None
    direccion: Optional[str] = None
    telefono: Optional[str] = None
    logo_url: Optional[str] = None
    zona_horaria: Optional[str] = None

@router.get("/club")
def get_club(db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    club = db.query(Club).filter(Club.id == current_admin.club_id).first()
    return {
        "id": str(club.id), "nombre": club.nombre, "direccion": club.direccion,
        "telefono": club.telefono, "logo_url": club.logo_url,
        "zona_horaria": club.zona_horaria, "whatsapp_from": club.whatsapp_from,
    }

@router.patch("/club")
def update_club(body: ClubUpdate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    club = db.query(Club).filter(Club.id == current_admin.club_id).first()
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(club, field, value)
    db.commit()
    return {"ok": True}

@router.get("/whatsapp/estado")
def whatsapp_estado(current_admin: Admin = Depends(get_current_user)):
    return {"conectado": True, "numero": "+52 834 247 2640", "proveedor": "twilio"}
