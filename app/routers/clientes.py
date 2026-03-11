from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, desc
from typing import Optional
from uuid import UUID
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Cliente, Reserva, Cancha, Admin
from app.schemas import ClienteCreate, ClienteUpdate, ClienteBloqueoUpdate

router = APIRouter(prefix="/clientes", tags=["clientes"])

@router.get("")
def list_clientes(
    search: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_admin: Admin = Depends(get_current_user)
):
    q = db.query(Cliente).filter(Cliente.club_id == current_admin.club_id)
    if search:
        q = q.filter(or_(Cliente.nombre.ilike(f"%{search}%"), Cliente.telefono.ilike(f"%{search}%")))
    total = q.count()
    clientes = q.order_by(Cliente.created_at.desc()).offset((page-1)*limit).limit(limit).all()
    result = []
    for c in clientes:
        total_res = db.query(func.count(Reserva.id)).filter(Reserva.cliente_id == c.id, Reserva.estado != "cancelada").scalar() or 0
        ultima = db.query(func.max(Reserva.fecha_inicio)).filter(Reserva.cliente_id == c.id).scalar()
        result.append({
            "id": str(c.id), "nombre": c.nombre, "telefono": c.telefono,
            "esta_bloqueado": c.esta_bloqueado, "motivo_bloqueo": c.motivo_bloqueo,
            "notas_internas": c.notas_internas, "created_at": c.created_at.isoformat(),
            "total_reservas": total_res, "ultima_reserva": ultima.isoformat() if ultima else None,
        })
    return {"total": total, "page": page, "limit": limit, "items": result}

@router.get("/{cliente_id}")
def get_cliente(cliente_id: UUID, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    c = db.query(Cliente).filter(Cliente.id == cliente_id, Cliente.club_id == current_admin.club_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    canchas = db.query(Cancha).filter(Cancha.club_id == current_admin.club_id).all()
    cancha_map = {can.id: can.nombre for can in canchas}
    cancha_ids = list(cancha_map.keys())
    reservas = db.query(Reserva).filter(Reserva.cliente_id == c.id, Reserva.cancha_id.in_(cancha_ids)).order_by(Reserva.fecha_inicio.desc()).limit(20).all()
    cancha_fav = db.query(Reserva.cancha_id, func.count(Reserva.id).label("cnt")).filter(
        Reserva.cliente_id == c.id, Reserva.estado != "cancelada"
    ).group_by(Reserva.cancha_id).order_by(desc("cnt")).first()
    cancelaciones = db.query(func.count(Reserva.id)).filter(Reserva.cliente_id == c.id, Reserva.estado == "cancelada").scalar() or 0
    return {
        "id": str(c.id), "nombre": c.nombre, "telefono": c.telefono,
        "esta_bloqueado": c.esta_bloqueado, "motivo_bloqueo": c.motivo_bloqueo,
        "notas_internas": c.notas_internas, "created_at": c.created_at.isoformat(),
        "stats": {
            "total_reservas": len(reservas), "cancelaciones": cancelaciones,
            "cancha_favorita": cancha_map.get(cancha_fav.cancha_id) if cancha_fav else None,
        },
        "historial": [{"id": str(r.id), "cancha_nombre": cancha_map.get(r.cancha_id,"—"),
            "fecha_inicio": r.fecha_inicio.isoformat(), "fecha_fin": r.fecha_fin.isoformat(),
            "estado": r.estado, "precio_total": float(r.precio_total) if r.precio_total else None,
        } for r in reservas]
    }

@router.post("", status_code=201)
def create_cliente(body: ClienteCreate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    if db.query(Cliente).filter(Cliente.telefono == body.telefono, Cliente.club_id == current_admin.club_id).first():
        raise HTTPException(status_code=409, detail="Ya existe un cliente con ese teléfono")
    cliente = Cliente(**body.model_dump(), club_id=current_admin.club_id)
    db.add(cliente); db.commit(); db.refresh(cliente)
    return {"id": str(cliente.id), "nombre": cliente.nombre, "telefono": cliente.telefono}

@router.patch("/{cliente_id}")
def update_cliente(cliente_id: UUID, body: ClienteUpdate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    c = db.query(Cliente).filter(Cliente.id == cliente_id, Cliente.club_id == current_admin.club_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(c, field, value)
    db.commit()
    return {"ok": True}

@router.patch("/{cliente_id}/bloqueo")
def toggle_bloqueo(cliente_id: UUID, body: ClienteBloqueoUpdate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    c = db.query(Cliente).filter(Cliente.id == cliente_id, Cliente.club_id == current_admin.club_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    c.esta_bloqueado = body.esta_bloqueado
    c.motivo_bloqueo = body.motivo_bloqueo if body.esta_bloqueado else None
    db.commit()
    return {"esta_bloqueado": c.esta_bloqueado}
