from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Reserva, Cancha, Cliente, Admin

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
TZ = ZoneInfo("America/Monterrey")

def now_local():
    return datetime.now(TZ)

@router.get("/metricas")
def get_metricas(
    fecha: str = Query(default=None),
    db: Session = Depends(get_db),
    current_admin: Admin = Depends(get_current_user)
):
    club_id = current_admin.club_id
    target = date.fromisoformat(fecha) if fecha else date.today()
    hoy_inicio = datetime(target.year, target.month, target.day, 0, 0, tzinfo=TZ)
    hoy_fin    = hoy_inicio + timedelta(days=1)
    ayer_inicio = hoy_inicio - timedelta(days=1)
    mes_inicio = datetime(target.year, target.month, 1, 0, 0, tzinfo=TZ)
    semana_inicio = hoy_inicio - timedelta(days=7)
    ahora = now_local()

    canchas = db.query(Cancha).filter(Cancha.club_id == club_id).all()
    cancha_ids = [c.id for c in canchas]

    reservas_hoy = db.query(func.count(Reserva.id)).filter(
        Reserva.cancha_id.in_(cancha_ids),
        Reserva.fecha_inicio >= hoy_inicio,
        Reserva.fecha_inicio < hoy_fin,
        Reserva.estado != "cancelada"
    ).scalar() or 0

    reservas_ayer = db.query(func.count(Reserva.id)).filter(
        Reserva.cancha_id.in_(cancha_ids),
        Reserva.fecha_inicio >= ayer_inicio,
        Reserva.fecha_inicio < hoy_inicio,
        Reserva.estado != "cancelada"
    ).scalar() or 0

    ocupadas_ahora = db.query(func.count(Reserva.id)).filter(
        Reserva.cancha_id.in_(cancha_ids),
        Reserva.fecha_inicio <= ahora,
        Reserva.fecha_fin >= ahora,
        Reserva.estado == "confirmada"
    ).scalar() or 0

    ingresos_mes = db.query(func.sum(Reserva.precio_total)).filter(
        Reserva.cancha_id.in_(cancha_ids),
        Reserva.fecha_inicio >= mes_inicio,
        Reserva.estado.in_(["confirmada", "completada"])
    ).scalar() or Decimal("0")

    clientes_nuevos = db.query(func.count(Cliente.id)).filter(
        Cliente.club_id == club_id,
        Cliente.created_at >= semana_inicio
    ).scalar() or 0

    return {
        "reservas_hoy": reservas_hoy,
        "reservas_ayer": reservas_ayer,
        "canchas_ocupadas_ahora": ocupadas_ahora,
        "total_canchas": len(canchas),
        "ingresos_mes": float(ingresos_mes),
        "clientes_nuevos_semana": clientes_nuevos,
    }

@router.get("/actividad-reciente")
def get_actividad(
    limit: int = Query(default=10, le=50),
    db: Session = Depends(get_db),
    current_admin: Admin = Depends(get_current_user)
):
    canchas = db.query(Cancha).filter(Cancha.club_id == current_admin.club_id).all()
    cancha_ids = [c.id for c in canchas]
    cancha_map = {c.id: c.nombre for c in canchas}
    reservas = db.query(Reserva).filter(
        Reserva.cancha_id.in_(cancha_ids)
    ).order_by(Reserva.created_at.desc()).limit(limit).all()
    return [{
        "id": str(r.id),
        "cliente_nombre": r.cliente.nombre if r.cliente else "—",
        "cliente_telefono": r.cliente.telefono if r.cliente else None,
        "cancha_nombre": cancha_map.get(r.cancha_id, "—"),
        "fecha_inicio": r.fecha_inicio.isoformat(),
        "fecha_fin": r.fecha_fin.isoformat(),
        "estado": r.estado,
        "origen": r.origen,
    } for r in reservas]

@router.get("/ocupacion-heatmap")
def get_heatmap(
    semana: str = Query(default=None),
    db: Session = Depends(get_db),
    current_admin: Admin = Depends(get_current_user)
):
    if semana:
        inicio = datetime.fromisoformat(semana).replace(tzinfo=TZ)
    else:
        hoy = date.today()
        lunes = hoy - timedelta(days=hoy.weekday())
        inicio = datetime(lunes.year, lunes.month, lunes.day, tzinfo=TZ)
    fin = inicio + timedelta(days=7)

    canchas = db.query(Cancha).filter(Cancha.club_id == current_admin.club_id).order_by(Cancha.orden).all()
    cancha_ids = [c.id for c in canchas]
    cancha_map = {c.id: c.nombre for c in canchas}

    reservas = db.query(Reserva).filter(
        Reserva.cancha_id.in_(cancha_ids),
        Reserva.fecha_inicio >= inicio,
        Reserva.fecha_inicio < fin,
        Reserva.estado != "cancelada"
    ).all()

    horas = [f"{h:02d}:00" for h in range(8, 22)]
    heatmap = {str(cid): {h: 0 for h in horas} for cid in cancha_ids}

    for r in reservas:
        h = r.fecha_inicio.astimezone(TZ).hour
        key = f"{h:02d}:00"
        cid = str(r.cancha_id)
        if cid in heatmap and key in heatmap[cid]:
            heatmap[cid][key] += 1

    result = []
    for cid in cancha_ids:
        for hora in horas:
            count = heatmap[str(cid)][hora]
            result.append({
                "cancha_id": str(cid),
                "cancha_nombre": cancha_map[cid],
                "hora": hora,
                "ocupacion": min(count / 7, 1.0),
                "count": count,
            })
    return result
