from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import re
from app.core.database import get_db
from app.models import Club, Cancha, Cliente, Reserva, Bloqueo

router = APIRouter(prefix="/webhook", tags=["webhook"])
TZ = ZoneInfo("America/Monterrey")
_sesiones: dict = {}

class Sesion:
    def __init__(self):
        self.paso = "elegir_cancha"
        self.cancha_id = None
        self.cancha_nombre = None
        self.fecha: Optional[date] = None
        self.hora_inicio: Optional[str] = None
        self.duracion_min: int = 60
        self.cliente_id = None

def twiml_reply(message: str) -> PlainTextResponse:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{message}</Message>\n</Response>"""
    return PlainTextResponse(content=xml, media_type="application/xml")

def fmt_hora(dt): return dt.astimezone(TZ).strftime("%-I:%M %p")
def fmt_fecha(dt):
    dias = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
    meses = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
    d = dt.astimezone(TZ)
    return f"{dias[d.weekday()]} {d.day} de {meses[d.month-1]}"

def parse_fecha(texto):
    texto = texto.strip().lower()
    hoy = date.today()
    if texto in ("hoy","hoy mismo"): return hoy
    if texto in ("mañana","manana"): return hoy + timedelta(days=1)
    dias = ["lunes","martes","miércoles","miercoles","jueves","viernes","sábado","sabado","domingo"]
    if texto in dias:
        idx = dias.index(texto) % 7
        diff = (idx - hoy.weekday()) % 7
        return hoy + timedelta(days=diff or 7)
    for fmt in ("%d/%m/%Y","%d/%m"):
        try:
            d = datetime.strptime(texto, fmt)
            if fmt == "%d/%m": d = d.replace(year=hoy.year)
            return d.date()
        except ValueError: pass
    return None

def parse_hora(texto):
    texto = texto.strip().lower().replace(" ","")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", texto)
    if not m: return None
    h, mins, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if meridiem == "pm" and h < 12: h += 12
    if meridiem == "am" and h == 12: h = 0
    if 0 <= h <= 23 and 0 <= mins <= 59: return f"{h:02d}:{mins:02d}"
    return None

def slots_disponibles(cancha, target, duracion, db):
    dia_key = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"][target.weekday()]
    horario = (cancha.horario or {}).get(dia_key, {})
    if not horario.get("activo", True): return []
    ah, am = map(int, horario.get("apertura","08:00").split(":"))
    ch, cm = map(int, horario.get("cierre","22:00").split(":"))
    inicio_dia = datetime(target.year, target.month, target.day, ah, am, tzinfo=TZ)
    fin_dia    = datetime(target.year, target.month, target.day, ch, cm, tzinfo=TZ)
    reservas = db.query(Reserva).filter(
        Reserva.cancha_id == cancha.id, Reserva.estado != "cancelada",
        Reserva.fecha_inicio >= inicio_dia, Reserva.fecha_inicio < fin_dia,
    ).all()
    bloqueos = db.query(Bloqueo).filter(
        Bloqueo.cancha_id == cancha.id, Bloqueo.fecha_inicio < fin_dia, Bloqueo.fecha_fin > inicio_dia,
    ).all()
    ocupados = [(r.fecha_inicio, r.fecha_fin) for r in reservas] + [(b.fecha_inicio, b.fecha_fin) for b in bloqueos]
    libres = []
    cur = inicio_dia
    while cur + timedelta(minutes=duracion) <= fin_dia:
        fin_slot = cur + timedelta(minutes=duracion)
        if all(not (cur < b_fin and fin_slot > b_ini) for b_ini, b_fin in ocupados):
            libres.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=duracion)
    return libres

@router.post("/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
    MessageSid: str = Form(default=""),
    db: Session = Depends(get_db)
):
    telefono = From.replace("whatsapp:","")
    texto = Body.strip()

    existe = db.query(Reserva).filter(Reserva.message_sid == MessageSid).first()
    if existe: return twiml_reply("✅ Tu mensaje ya fue procesado.")

    to_number = request.headers.get("x-twilio-to","").replace("whatsapp:","")
    club = db.query(Club).filter(Club.whatsapp_from == to_number).first()
    if not club: club = db.query(Club).filter(Club.is_active == True).first()
    if not club: return twiml_reply("Lo sentimos, número no configurado.")

    cliente = db.query(Cliente).filter(Cliente.telefono == telefono, Cliente.club_id == club.id).first()
    sesion = _sesiones.get(telefono, Sesion())

    if texto.lower() in ("hola","hi","hello","reservar","1","inicio","menu","menú"):
        sesion = Sesion()
        _sesiones[telefono] = sesion
        canchas = db.query(Cancha).filter(Cancha.club_id == club.id, Cancha.estado == "activa").order_by(Cancha.orden).all()
        nombre = f" {cliente.nombre.split()[0]}" if cliente else ""
        lista = "\n".join([f"{i+1}. {c.nombre}" for i,c in enumerate(canchas)])
        return twiml_reply(f"👋 ¡Hola{nombre}! Bienvenido a *{club.nombre}*.\n\n¿Qué cancha quieres reservar?\n\n{lista}\n\nResponde con el número.")

    if texto.lower() in ("cancelar","salir"):
        _sesiones.pop(telefono, None)
        return twiml_reply("👍 Cancelado. Escribe *hola* cuando quieras reservar.")

    if texto.lower() in ("mis reservas","2"):
        if not cliente: return twiml_reply("No tienes reservas aún. Escribe *hola* para hacer una.")
        proximas = db.query(Reserva).filter(
            Reserva.cliente_id == cliente.id, Reserva.fecha_inicio >= datetime.now(TZ), Reserva.estado != "cancelada"
        ).order_by(Reserva.fecha_inicio).limit(3).all()
        if not proximas: return twiml_reply("No tienes reservas próximas. Escribe *hola* para hacer una.")
        lineas = []
        for r in proximas:
            c = db.query(Cancha).filter(Cancha.id == r.cancha_id).first()
            lineas.append(f"📅 {fmt_fecha(r.fecha_inicio)} {fmt_hora(r.fecha_inicio)}–{fmt_hora(r.fecha_fin)} — {c.nombre if c else '?'}")
        return twiml_reply("*Tus próximas reservas:*\n\n" + "\n".join(lineas))

    if sesion.paso == "elegir_cancha":
        canchas = db.query(Cancha).filter(Cancha.club_id == club.id, Cancha.estado == "activa").order_by(Cancha.orden).all()
        try:
            cancha = canchas[int(texto)-1]
        except (ValueError, IndexError):
            lista = "\n".join([f"{i+1}. {c.nombre}" for i,c in enumerate(canchas)])
            return twiml_reply(f"Elige un número válido:\n\n{lista}")
        sesion.cancha_id = cancha.id
        sesion.cancha_nombre = cancha.nombre
        sesion.paso = "elegir_fecha"
        _sesiones[telefono] = sesion
        return twiml_reply(f"✅ *{cancha.nombre}* seleccionada.\n\n¿Para qué fecha?\n• *hoy*\n• *mañana*\n• *lunes*\n• *15/06*")

    if sesion.paso == "elegir_fecha":
        fecha = parse_fecha(texto)
        if not fecha: return twiml_reply("No entendí la fecha. Escribe *hoy*, *mañana*, *lunes* o *15/06*.")
        if fecha < date.today(): return twiml_reply("Esa fecha ya pasó. ¿Para qué día?")
        cancha = db.query(Cancha).filter(Cancha.id == sesion.cancha_id).first()
        libres = slots_disponibles(cancha, fecha, sesion.duracion_min, db)
        if not libres:
            return twiml_reply(f"😔 No hay horarios disponibles el {fecha.strftime('%d/%m')}. Prueba otra fecha.")
        sesion.fecha = fecha
        sesion.paso = "elegir_hora"
        _sesiones[telefono] = sesion
        cols = [libres[i:i+4] for i in range(0, len(libres), 4)]
        tabla = "\n".join(["   ".join(fila) for fila in cols])
        return twiml_reply(f"📅 *{fmt_fecha(datetime(fecha.year,fecha.month,fecha.day,tzinfo=TZ))}*\n\nHorarios disponibles:\n\n{tabla}\n\n¿A qué hora?")

    if sesion.paso == "elegir_hora":
        hora = parse_hora(texto)
        if not hora: return twiml_reply("No entendí. Escribe algo como *10:00* o *4pm*.")
        cancha = db.query(Cancha).filter(Cancha.id == sesion.cancha_id).first()
        libres = slots_disponibles(cancha, sesion.fecha, sesion.duracion_min, db)
        if hora not in libres: return twiml_reply(f"⚠️ No disponible. Elige entre:\n{', '.join(libres)}")
        sesion.hora_inicio = hora
        sesion.paso = "confirmar_nombre" if not cliente else "confirmar_reserva"
        _sesiones[telefono] = sesion
        if not cliente: return twiml_reply("¿Cuál es tu nombre completo?")
        h, m = map(int, hora.split(":"))
        inicio = datetime(sesion.fecha.year, sesion.fecha.month, sesion.fecha.day, h, m, tzinfo=TZ)
        fin = inicio + timedelta(minutes=sesion.duracion_min)
        precio = float(cancha.precio_hora) * sesion.duracion_min / 60
        return twiml_reply(f"Confirma tu reserva:\n\n🎾 *{cancha.nombre}*\n📅 {fmt_fecha(inicio)}\n⏰ {fmt_hora(inicio)} – {fmt_hora(fin)}\n💰 ${precio:,.0f} MXN\n\nResponde *sí* o *no*.")

    if sesion.paso == "confirmar_nombre":
        if len(texto.strip()) < 2: return twiml_reply("Por favor escribe tu nombre completo.")
        cliente = Cliente(club_id=club.id, nombre=texto.strip(), telefono=telefono)
        db.add(cliente); db.flush()
        sesion.cliente_id = cliente.id
        sesion.paso = "confirmar_reserva"
        _sesiones[telefono] = sesion
        cancha = db.query(Cancha).filter(Cancha.id == sesion.cancha_id).first()
        h, m = map(int, sesion.hora_inicio.split(":"))
        inicio = datetime(sesion.fecha.year, sesion.fecha.month, sesion.fecha.day, h, m, tzinfo=TZ)
        fin = inicio + timedelta(minutes=sesion.duracion_min)
        precio = float(cancha.precio_hora) * sesion.duracion_min / 60
        return twiml_reply(f"Confirma, *{texto.strip()}*:\n\n🎾 *{cancha.nombre}*\n📅 {fmt_fecha(inicio)}\n⏰ {fmt_hora(inicio)} – {fmt_hora(fin)}\n💰 ${precio:,.0f} MXN (pago en el club)\n\nResponde *sí* o *no*.")

    if sesion.paso == "confirmar_reserva":
        if texto.lower() in ("sí","si","yes","s","ok","dale","confirmar"):
            cancha = db.query(Cancha).filter(Cancha.id == sesion.cancha_id).first()
            h, m = map(int, sesion.hora_inicio.split(":"))
            inicio = datetime(sesion.fecha.year, sesion.fecha.month, sesion.fecha.day, h, m, tzinfo=TZ)
            fin = inicio + timedelta(minutes=sesion.duracion_min)
            if not cliente: cliente = db.query(Cliente).filter(Cliente.id == sesion.cliente_id).first()
            if cliente.esta_bloqueado:
                _sesiones.pop(telefono, None)
                return twiml_reply("Lo sentimos, tu cuenta no puede hacer reservas. Comunícate con el club.")
            precio = float(cancha.precio_hora) * sesion.duracion_min / 60
            reserva = Reserva(
                cancha_id=cancha.id, cliente_id=cliente.id,
                fecha_inicio=inicio, fecha_fin=fin, duracion_min=sesion.duracion_min,
                precio_total=round(precio,2), estado="confirmada", origen="whatsapp", message_sid=MessageSid,
            )
            db.add(reserva); db.commit()
            _sesiones.pop(telefono, None)
            return twiml_reply(f"✅ *¡Reserva confirmada!*\n\n🎾 {cancha.nombre}\n📅 {fmt_fecha(inicio)}\n⏰ {fmt_hora(inicio)} – {fmt_hora(fin)}\n💰 ${precio:,.0f} MXN (pago en el club)\n\n¡Te esperamos en *{club.nombre}*! 🏓")
        else:
            _sesiones.pop(telefono, None)
            return twiml_reply("❌ Cancelado. Escribe *hola* para intentar de nuevo.")

    _sesiones.pop(telefono, None)
    return twiml_reply("Escribe *hola* para reservar o *mis reservas* para ver las tuyas.")
