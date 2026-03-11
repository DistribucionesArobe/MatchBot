from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional
from uuid import UUID
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import csv, io
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Reserva, Cancha, Cliente, Admin
from app.schemas import ReservaCreate, ReservaUpdate

router = APIRouter(prefix="/reservas", tags=["reservas"])
TZ = ZoneInfo("America/Monterrey")

def _cancha_ids(club_id, db):
    return [c.id for c in db.query(Cancha).filter(Cancha.club_id == club_id).all()]

def _cancha_map(club_id, db):
    return {c.id: c.nombre for c in db.query(Cancha).filter(Cancha.club_id == club_id).all()}

def _check_disponible(cancha_id, inicio, fin, db, exclude_id=None):
    q = db.query(Reserva).filter(
        Reserva.cancha_id == cancha_id, Reserva.estado != "cancelada",
        Reserva.fecha_inicio < fin, Reserva.fecha_fin > inicio,
    )
    if exclude_id: q = q.filter(Reserva.id != exclude_id)
    return q.first() is None

def _fmt(r, cancha_map):
    return {
        "id": str(r.id), "cancha_id": str(r.cancha_id),
        "cancha_nombre": cancha_map.get(r.cancha_id,"—"),
        "cliente_id": str(r.cliente_id),
        "cliente_nombre": r.cliente.nombre if r.cliente else "—",
        "cliente_telefono": r.cliente.telefono if r.cliente else None,
        "fecha_inicio": r.fecha_inicio.isoformat(), "fecha_fin": r.fecha_fin.isoformat(),
        "duracion_min": r.duracion_min,
        "precio_total": float(r.precio_total) if r.precio_total else None,
        "estado": r.estado, "origen": r.origen,
        "nota_interna": r.nota_interna, "created_at": r.created_at.isoformat(),
    }

@router.get("/disponibilidad")
def disponibilidad(
    cancha_id: UUID = Query(...), fecha: str = Query(...), duracion_min: int = Query(default=60),
    db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)
):
    cancha = db.query(Cancha).filter(Cancha.id == cancha_id, Cancha.club_id == current_admin.club_id).first()
    if not cancha: raise HTTPException(status_code=404, detail="Cancha no encontrada")
    target = date.fromisoformat(fecha)
    dia = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"][target.weekday()]
    horario = (cancha.horario or {}).get(dia, {})
    if not horario.get("activo", True): return {"slots": [], "cancha_cerrada": True}
    ah, am = map(int, horario.get("apertura","08:00").split(":"))
    ch, cm = map(int, horario.get("cierre","22:00").split(":"))
    apertura = datetime(target.year, target.month, target.day, ah, am, tzinfo=TZ)
    cierre   = datetime(target.year, target.month, target.day, ch, cm, tzinfo=TZ)
    slots = []
    cur = apertura
    while cur + timedelta(minutes=duracion_min) <= cierre:
        fin = cur + timedelta(minutes=duracion_min)
        slots.append({"hora_inicio": cur.strftime("%H:%M"), "hora_fin": fin.strftime("%H:%M"), "disponible": _check_disponible(cancha_id, cur, fin, db)})
        cur += timedelta(minutes=duracion_min)
    return {"slots": slots, "cancha_cerrada": False}

@router.get("/exportar-csv")
def export_csv(
    semana: Optional[str] = None, cancha_id: Optional[UUID] = None, estado: Optional[str] = None,
    db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)
):
    ids = _cancha_ids(current_admin.club_id, db)
    cmap = _cancha_map(current_admin.club_id, db)
    q = db.query(Reserva).filter(Reserva.cancha_id.in_(ids))
    if semana:
        inicio = datetime.fromisoformat(semana).replace(tzinfo=TZ)
        q = q.filter(Reserva.fecha_inicio >= inicio, Reserva.fecha_inicio < inicio + timedelta(days=7))
    if cancha_id: q = q.filter(Reserva.cancha_id == cancha_id)
    if estado: q = q.filter(Reserva.estado == estado)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Cliente","Teléfono","Cancha","Inicio","Fin","Duración","Estado","Origen"])
    for r in q.order_by(Reserva.fecha_inicio).all():
        writer.writerow([str(r.id), r.cliente.nombre if r.cliente else "", r.cliente.telefono if r.cliente else "",
            cmap.get(r.cancha_id,""), r.fecha_inicio.strftime("%Y-%m-%d %H:%M"), r.fecha_fin.strftime("%Y-%m-%d %H:%M"),
            r.duracion_min, r.estado, r.origen])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reservas.csv"})

@router.get("")
def list_reservas(
    semana: Optional[str] = None, cancha_id: Optional[UUID] = None,
    estado: Optional[str] = None, search: Optional[str] = None,
    page: int = 1, limit: int = 50,
    db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)
):
    ids = _cancha_ids(current_admin.club_id, db)
    cmap = _cancha_map(current_admin.club_id, db)
    q = db.query(Reserva).filter(Reserva.cancha_id.in_(ids))
    if semana:
        inicio = datetime.fromisoformat(semana).replace(tzinfo=TZ)
        q = q.filter(Reserva.fecha_inicio >= inicio, Reserva.fecha_inicio < inicio + timedelta(days=7))
    if cancha_id: q = q.filter(Reserva.cancha_id == cancha_id)
    if estado: q = q.filter(Reserva.estado == estado)
    if search:
        sub = db.query(Cliente.id).filter(or_(Cliente.nombre.ilike(f"%{search}%"), Cliente.telefono.ilike(f"%{search}%"))).subquery()
        q = q.filter(Reserva.cliente_id.in_(sub))
    total = q.count()
    reservas = q.order_by(Reserva.fecha_inicio.desc()).offset((page-1)*limit).limit(limit).all()
    return {"total": total, "page": page, "limit": limit, "items": [_fmt(r, cmap) for r in reservas]}

@router.get("/{reserva_id}")
def get_reserva(reserva_id: UUID, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    ids = _cancha_ids(current_admin.club_id, db)
    cmap = _cancha_map(current_admin.club_id, db)
    r = db.query(Reserva).filter(Reserva.id == reserva_id, Reserva.cancha_id.in_(ids)).first()
    if not r: raise HTTPException(status_code=404, detail="Reserva no encontrada")
    return _fmt(r, cmap)

@router.post("", status_code=201)
def create_reserva(body: ReservaCreate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    cancha = db.query(Cancha).filter(Cancha.id == body.cancha_id, Cancha.club_id == current_admin.club_id).first()
    if not cancha: raise HTTPException(status_code=404, detail="Cancha no encontrada")
    if cancha.estado != "activa": raise HTTPException(status_code=409, detail=f"Cancha {cancha.estado}")
    inicio = body.fecha_inicio
    fin = inicio + timedelta(minutes=body.duracion_min)
    if not _check_disponible(body.cancha_id, inicio, fin, db):
        raise HTTPException(status_code=409, detail="Horario no disponible")
    if body.cliente_id:
        cliente = db.query(Cliente).filter(Cliente.id == body.cliente_id, Cliente.club_id == current_admin.club_id).first()
        if not cliente: raise HTTPException(status_code=404, detail="Cliente no encontrado")
    elif body.cliente_telefono:
        cliente = db.query(Cliente).filter(Cliente.telefono == body.cliente_telefono, Cliente.club_id == current_admin.club_id).first()
        if not cliente:
            cliente = Cliente(club_id=current_admin.club_id, nombre=body.cliente_nombre or body.cliente_telefono, telefono=body.cliente_telefono)
            db.add(cliente); db.flush()
    else:
        raise HTTPException(status_code=422, detail="Se requiere cliente_id o cliente_telefono")
    if cliente.esta_bloqueado: raise HTTPException(status_code=403, detail="Cliente bloqueado")
    precio = float(cancha.precio_hora_finde or cancha.precio_hora) * body.duracion_min / 60 if inicio.weekday() >= 5 else float(cancha.precio_hora) * body.duracion_min / 60
    reserva = Reserva(cancha_id=body.cancha_id, cliente_id=cliente.id, fecha_inicio=inicio, fecha_fin=fin,
        duracion_min=body.duracion_min, precio_total=round(precio,2), estado="confirmada", origen=body.origen, nota_interna=body.nota_interna)
    db.add(reserva); db.commit(); db.refresh(reserva)
    return _fmt(reserva, _cancha_map(current_admin.club_id, db))

@router.patch("/{reserva_id}")
def update_reserva(reserva_id: UUID, body: ReservaUpdate, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    ids = _cancha_ids(current_admin.club_id, db)
    r = db.query(Reserva).filter(Reserva.id == reserva_id, Reserva.cancha_id.in_(ids)).first()
    if not r: raise HTTPException(status_code=404, detail="Reserva no encontrada")
    if body.fecha_inicio and body.duracion_min:
        nuevo_fin = body.fecha_inicio + timedelta(minutes=body.duracion_min)
        if not _check_disponible(r.cancha_id, body.fecha_inicio, nuevo_fin, db, exclude_id=reserva_id):
            raise HTTPException(status_code=409, detail="Horario no disponible")
        r.fecha_inicio = body.fecha_inicio; r.fecha_fin = nuevo_fin; r.duracion_min = body.duracion_min
    if body.estado: r.estado = body.estado
    if body.nota_interna is not None: r.nota_interna = body.nota_interna
    db.commit(); db.refresh(r)
    return _fmt(r, _cancha_map(current_admin.club_id, db))

@router.delete("/{reserva_id}", status_code=204)
def delete_reserva(reserva_id: UUID, db: Session = Depends(get_db), current_admin: Admin = Depends(get_current_user)):
    ids = _cancha_ids(current_admin.club_id, db)
    r = db.query(Reserva).filter(Reserva.id == reserva_id, Reserva.cancha_id.in_(ids)).first()
    if not r: raise HTTPException(status_code=404, detail="Reserva no encontrada")
    r.estado = "cancelada"; db.commit()
