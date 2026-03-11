from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from uuid import UUID
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Cancha, Bloqueo, Admin
from app.schemas import CanchaCreate, CanchaUpdate, CanchaOut, CanchaEstadoUpdate, BloqueoCreate, BloqueoOut

router = APIRouter(prefix="/canchas", tags=["canchas"])

def _get_cancha_or_404(cancha_id, club_id, db):
    c = db.query(Cancha).filter(Cancha.id == cancha_id, Cancha.club_id == club_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cancha no encontrada")
    return c

@router.get("", response_model=List[CanchaOut])
def list_canchas(db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    return db.query(Cancha).filter(Cancha.club_id == current_admin.club_id).order_by(Cancha.orden).all()

@router.post("", response_model=CanchaOut, status_code=201)
def create_cancha(body: CanchaCreate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    cancha = Cancha(**body.model_dump(), club_id=current_admin.club_id)
    db.add(cancha); db.commit(); db.refresh(cancha)
    return cancha

@router.patch("/{cancha_id}", response_model=CanchaOut)
def update_cancha(cancha_id: UUID, body: CanchaUpdate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    cancha = _get_cancha_or_404(cancha_id, current_admin.club_id, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(cancha, field, value)
    db.commit(); db.refresh(cancha)
    return cancha

@router.patch("/{cancha_id}/estado", response_model=CanchaOut)
def update_estado(cancha_id: UUID, body: CanchaEstadoUpdate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    if body.estado not in ("activa", "cerrada", "mantenimiento"):
        raise HTTPException(status_code=422, detail="Estado inválido")
    cancha = _get_cancha_or_404(cancha_id, current_admin.club_id, db)
    cancha.estado = body.estado
    db.commit(); db.refresh(cancha)
    return cancha

@router.delete("/{cancha_id}", status_code=204)
def delete_cancha(cancha_id: UUID, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    cancha = _get_cancha_or_404(cancha_id, current_admin.club_id, db)
    db.delete(cancha); db.commit()

@router.get("/bloqueos")
def list_bloqueos(db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    canchas = db.query(Cancha).filter(Cancha.club_id == current_admin.club_id).all()
    cancha_ids = [c.id for c in canchas]
    cancha_map = {c.id: c.nombre for c in canchas}
    bloqueos = db.query(Bloqueo).filter(Bloqueo.cancha_id.in_(cancha_ids)).order_by(Bloqueo.fecha_inicio).all()
    return [{
        "id": str(b.id),
        "cancha_id": str(b.cancha_id),
        "cancha_nombre": cancha_map.get(b.cancha_id),
        "fecha_inicio": b.fecha_inicio.isoformat(),
        "fecha_fin": b.fecha_fin.isoformat(),
        "motivo": b.motivo,
        "created_at": b.created_at.isoformat(),
    } for b in bloqueos]

@router.post("/bloqueos", status_code=201)
def create_bloqueos(body: BloqueoCreate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    canchas = db.query(Cancha).filter(Cancha.club_id == current_admin.club_id).all()
    club_cancha_ids = {c.id for c in canchas}
    for cid in body.cancha_ids:
        if cid not in club_cancha_ids:
            raise HTTPException(status_code=403, detail=f"Cancha {cid} no pertenece al club")
        db.add(Bloqueo(cancha_id=cid, fecha_inicio=body.fecha_inicio, fecha_fin=body.fecha_fin, motivo=body.motivo))
    db.commit()
    return {"creados": len(body.cancha_ids)}

@router.delete("/bloqueos/{bloqueo_id}", status_code=204)
def delete_bloqueo(bloqueo_id: UUID, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    canchas = db.query(Cancha).filter(Cancha.club_id == current_admin.club_id).all()
    cancha_ids = {c.id for c in canchas}
    bloqueo = db.query(Bloqueo).filter(Bloqueo.id == bloqueo_id).first()
    if not bloqueo or bloqueo.cancha_id not in cancha_ids:
        raise HTTPException(status_code=404, detail="Bloqueo no encontrado")
    db.delete(bloqueo); db.commit()
