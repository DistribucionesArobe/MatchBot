"""
MatchBot — Panel de administración estilo Playtomic Manager.
Secciones: Calendario, Clientes, Premios, Pagos, Reservas, Reportes.
Tema claro, sidebar izquierdo, grid de canchas × horas.
"""
import os
import logging
from datetime import datetime, timedelta, date as date_cls

from db.database import execute

logger = logging.getLogger("matchbot.panel")

OFFSET = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
DAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MONTHS = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

GRID_START_MIN = 6 * 60 + 30   # 06:30
GRID_END_MIN = 23 * 60 + 30    # 23:30
PX_PER_MIN = 1.0


def _esc(s) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _local(dt_utc: datetime) -> datetime:
    return dt_utc + timedelta(hours=OFFSET)


def _fmt_hhmm(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0").lower()


def _parse_dt(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(iso)[:19])
    except (ValueError, TypeError):
        return None


def _match_price(m: dict) -> str:
    """Total price from registrations ('75 MXN' x4 → $300)."""
    try:
        total = 0
        found = False
        for reg in (m.get("registration_info") or {}).get("registrations", []):
            p = reg.get("price") or ""
            digits = "".join(ch for ch in str(p) if ch.isdigit() or ch == ".")
            if digits:
                total += float(digits)
                found = True
        if found:
            return f"${total:.0f}"
    except Exception:
        pass
    return "—"


def _match_row(m: dict) -> dict | None:
    """Normalize a Playtomic match for display."""
    start = _parse_dt(m.get("start_date"))
    end = _parse_dt(m.get("end_date"))
    if not start:
        return None
    start_l = _local(start)
    dur = int((end - start).total_seconds() // 60) if end else 90
    players, is_bot = [], False
    for t in m.get("teams", []):
        for p in t.get("players", []):
            nm = p.get("name") or ""
            if nm:
                players.append(nm)
            mid = str(p.get("merchant_player_id") or "")
            if mid.startswith("guest:") and "(" in nm:
                is_bot = True
    pay = str((m.get("registration_info") or {}).get("payments_status", "")).upper()
    pname = (", ".join(players) or "Reserva")
    lower = pname.lower()
    sport = str(m.get("sport_id", ""))
    if "cerrado" in lower or "bloqueo" in lower or "mantenimiento" in lower:
        kind = "cerrado"
    elif "clase" in lower or "academia" in lower or "campamento" in lower or "torneo" in lower:
        kind = "clase"
    elif "FOOT" in sport.upper():
        kind = "futbol"
    elif is_bot:
        kind = "bot"
    else:
        kind = "normal"
    return {
        "start_l": start_l,
        "dur": dur,
        "court": m.get("resource_name", "?"),
        "players": pname,
        "bot": is_bot,
        "paid": pay == "PAID",
        "price": _match_price(m),
        "status": str(m.get("status", "")).upper(),
        "sport": sport,
        "kind": kind,
    }


# ─────────────────────────────────────────────────────
# SECCIONES
# ─────────────────────────────────────────────────────

def calendar_html(courts: list[str], day_matches: list[dict], day: date_cls) -> str:
    """Grid: canchas como columnas, horas como filas, bloques verdes."""
    total_min = GRID_END_MIN - GRID_START_MIN
    height = int(total_min * PX_PER_MIN)

    # Time gutter (hour marks)
    gutter = ""
    t = GRID_START_MIN
    while t <= GRID_END_MIN:
        top = int((t - GRID_START_MIN) * PX_PER_MIN)
        hh, mm = divmod(t, 60)
        ampm = "am" if hh < 12 else "pm"
        h12 = hh % 12 or 12
        gutter += f'<div class="tmark" style="top:{top}px">{h12}:{mm:02d} {ampm}</div>'
        t += 60

    cols = ""
    for court in courts:
        blocks = ""
        for b in day_matches:
            if b["court"] != court or b["start_l"].date() != day:
                continue
            if "CANCEL" in b["status"]:
                continue
            start_min = b["start_l"].hour * 60 + b["start_l"].minute
            top = int(max(0, (start_min - GRID_START_MIN)) * PX_PER_MIN)
            h = int(b["dur"] * PX_PER_MIN) - 2
            pay_pill = '<span class="pill paid">Pagado</span>' if b["paid"] else '<span class="pill unpaid">Unpaid</span>'
            bot_tag = ' <span class="pill botp">🤖</span>' if b["bot"] else ""
            end_l = b["start_l"] + timedelta(minutes=b["dur"])
            blocks += (
                f'<div class="block k-{b.get("kind", "normal")}" style="top:{top}px;height:{h}px">'
                f'{pay_pill}{bot_tag}'
                f'<div class="bname">{_esc(b["players"][:40])}</div>'
                f'<div class="btime">{_fmt_hhmm(b["start_l"])} - {_fmt_hhmm(end_l)}</div>'
                f'</div>'
            )
        cols += (
            f'<div class="court-col"><div class="court-head">{_esc(court)}</div>'
            f'<div class="court-body" style="height:{height}px">{blocks}</div></div>'
        )

    legend = (
        '<div class="legend">'
        '<span><i class="sw k-normal"></i> Reserva club</span>'
        '<span><i class="sw k-bot"></i> Reserva del bot</span>'
        '<span><i class="sw k-futbol"></i> Fútbol</span>'
        '<span><i class="sw k-clase"></i> Clase / academia</span>'
        '<span><i class="sw k-cerrado"></i> Cerrado</span>'
        '</div>'
    )
    return legend + (
        f'<div class="cal-wrap">'
        f'<div class="gutter"><div class="court-head"></div>'
        f'<div class="gutter-body" style="height:{height}px">{gutter}</div></div>'
        f'{cols}</div>'
    )


def clientes_html() -> str:
    try:
        rows = execute("""
            SELECT lb.wa_phone,
                   COUNT(*) FILTER (WHERE lb.status='stamped')   AS sellos_total,
                   COUNT(*) FILTER (WHERE lb.status='pending')   AS pendientes,
                   COUNT(*)                                       AS reservas,
                   MAX(lb.created_at)                             AS ultima
            FROM loyalty_bookings lb
            GROUP BY lb.wa_phone
            ORDER BY reservas DESC
            LIMIT 100
        """, fetch_all=True) or []
    except Exception as e:
        logger.error(f"panel clientes query: {e}")
        rows = []

    names = {}
    try:
        for r in execute("SELECT wa_phone, data->>'customer_name' AS n FROM wa_booking_state", fetch_all=True) or []:
            if r.get("n"):
                names[r["wa_phone"]] = r["n"]
    except Exception:
        pass

    if not rows:
        return '<div class="empty">Aún no hay clientes registrados por el bot. Aparecerán conforme reserven.</div>'

    body = ""
    for r in rows:
        phone = r["wa_phone"]
        cycle = int(r["sellos_total"]) % 10
        body += (
            f'<tr><td><b>{_esc(names.get(phone, "Cliente"))}</b></td>'
            f'<td>+{_esc(phone)}</td>'
            f'<td>{r["reservas"]}</td>'
            f'<td>🎾 {cycle}/10</td>'
            f'<td>{r["ultima"].strftime("%d/%m/%Y") if r["ultima"] else "—"}</td></tr>'
        )
    return (
        '<table class="tbl"><tr><th>Cliente</th><th>Teléfono</th>'
        '<th>Reservas bot</th><th>Tarjeta</th><th>Última</th></tr>' + body + '</table>'
    )


def premios_html() -> str:
    try:
        rows = execute("""
            SELECT wa_phone, reward_type, earned_at, used_at
            FROM loyalty_rewards ORDER BY earned_at DESC LIMIT 100
        """, fetch_all=True) or []
    except Exception as e:
        logger.error(f"panel premios query: {e}")
        rows = []
    if not rows:
        return '<div class="empty">Todavía nadie gana premios. Al sello 5: mitad de precio · Al 10: cancha gratis.</div>'
    body = ""
    for r in rows:
        tipo = "🆓 Cancha GRATIS (10 sellos)" if r["reward_type"] == "free" else "½ Mitad de precio (5 sellos)"
        estado = ('<span class="pill paid">Usado</span>' if r["used_at"]
                  else '<span class="pill unpaid">Pendiente</span>')
        body += (
            f'<tr><td>+{_esc(r["wa_phone"])}</td><td>{tipo}</td>'
            f'<td>{r["earned_at"].strftime("%d/%m/%Y") if r["earned_at"] else "—"}</td>'
            f'<td>{estado}</td></tr>'
        )
    return ('<table class="tbl"><tr><th>Cliente</th><th>Premio</th>'
            '<th>Ganado</th><th>Estado</th></tr>' + body + '</table>')


def pagos_html(upcoming: list[dict]) -> str:
    if not upcoming:
        return '<div class="empty">No hay reservas próximas.</div>'
    body = ""
    for b in upcoming:
        estado = ('<span class="pill paid">Pagado</span>' if b["paid"]
                  else '<span class="pill unpaid">Por cobrar</span>')
        body += (
            f'<tr><td>{DAYS[b["start_l"].weekday()][:3]} {b["start_l"].day}/{b["start_l"].month} · {_fmt_hhmm(b["start_l"])}</td>'
            f'<td>{_esc(b["court"])}</td><td>{_esc(b["players"][:35])}</td>'
            f'<td><b>{b["price"]}</b></td><td>{estado}</td></tr>'
        )
    return ('<table class="tbl"><tr><th>Cuándo</th><th>Cancha</th>'
            '<th>Cliente</th><th>Precio</th><th>Pago</th></tr>' + body + '</table>')


def reservas_html(upcoming: list[dict]) -> str:
    if not upcoming:
        return '<div class="empty">No hay reservas próximas.</div>'
    body = ""
    last_day = None
    for b in upcoming:
        d = b["start_l"].date()
        if d != last_day:
            last_day = d
            body += (f'<tr><td colspan="4" class="dayhead">'
                     f'{DAYS[d.weekday()]} {d.day} {MONTHS[d.month]}</td></tr>')
        origen = '<span class="pill botp">🤖 Bot</span>' if b["bot"] else '<span class="pill clubp">🏢 Club</span>'
        body += (
            f'<tr><td class="hora">{_fmt_hhmm(b["start_l"])}</td>'
            f'<td>{"⚽" if "FOOT" in b["sport"] else "🎾"} {_esc(b["court"])}</td>'
            f'<td>{_esc(b["players"][:40])}</td><td>{origen}</td></tr>'
        )
    return '<table class="tbl">' + body + '</table>'


def reportes_html() -> str:
    def _q(sql):
        try:
            return execute(sql, fetch_one=True) or {}
        except Exception:
            return {}
    usuarios = _q("SELECT COUNT(DISTINCT wa_phone) AS n FROM wa_booking_state").get("n", 0)
    activos7 = _q("SELECT COUNT(DISTINCT wa_phone) AS n FROM wa_booking_state WHERE updated_at >= NOW() - INTERVAL '7 days'").get("n", 0)
    res_total = _q("SELECT COUNT(*) AS n FROM loyalty_bookings").get("n", 0)
    res_7d = _q("SELECT COUNT(*) AS n FROM loyalty_bookings WHERE created_at >= NOW() - INTERVAL '7 days'").get("n", 0)
    jugadas = _q("SELECT COUNT(*) AS n FROM loyalty_bookings WHERE status='stamped'").get("n", 0)
    cancel = _q("SELECT COUNT(*) AS n FROM loyalty_bookings WHERE status='cancelled'").get("n", 0)
    premios = _q("SELECT COUNT(*) AS n FROM loyalty_rewards").get("n", 0)

    cards = [
        (usuarios, "Clientes que han usado el bot"),
        (activos7, "Activos últimos 7 días"),
        (res_total, "Reservas por el bot (total)"),
        (res_7d, "Reservas últimos 7 días"),
        (jugadas, "Partidos jugados (sellos)"),
        (cancel, "Cancelaciones"),
        (premios, "Premios ganados"),
    ]
    html = '<div class="cards">'
    for num, lbl in cards:
        html += f'<div class="card"><div class="num">{num}</div><div class="lbl">{lbl}</div></div>'
    html += '</div><p class="hint">Para ver el detalle del uso del bot (gráficas por día): <a href="/stats">/stats</a></p>'
    return html


# ─────────────────────────────────────────────────────
# PÁGINA
# ─────────────────────────────────────────────────────

PAGE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MatchBot Manager — Club de Padel Victoria</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f6f7f9; color:#0f172a; display:flex; min-height:100vh; }
.sidebar { width:200px; background:white; border-right:1px solid #e5e7eb; padding:1rem 0; flex-shrink:0; }
.brand { font-weight:800; font-size:1rem; padding:0 1.25rem 1rem; color:#0f172a; }
.brand span { color:#10b981; }
.nav-item { display:flex; align-items:center; gap:0.6rem; padding:0.65rem 1.25rem; font-size:0.85rem; color:#374151; cursor:pointer; border-left:3px solid transparent; text-decoration:none; }
.nav-item:hover { background:#f3f4f6; }
.nav-item.active { background:#ecfdf5; border-left-color:#10b981; color:#047857; font-weight:700; }
.main { flex:1; padding:1.25rem 1.5rem; overflow-x:auto; }
.topbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem; flex-wrap:wrap; gap:0.5rem; }
.topbar h1 { font-size:1.1rem; }
.datenav { display:flex; align-items:center; gap:0.4rem; }
.datenav a, .datenav span.cur { background:white; border:1px solid #e5e7eb; border-radius:8px; padding:0.4rem 0.8rem; font-size:0.8rem; text-decoration:none; color:#0f172a; font-weight:600; }
.datenav a:hover { border-color:#10b981; }
section { display:none; }
section.visible { display:block; }
/* Calendario */
.cal-wrap { display:flex; background:white; border:1px solid #e5e7eb; border-radius:12px; overflow-x:auto; }
.gutter { width:70px; flex-shrink:0; border-right:1px solid #f3f4f6; }
.gutter-body { position:relative; }
.tmark { position:absolute; right:8px; font-size:0.65rem; color:#9ca3af; transform:translateY(-50%); }
.court-col { flex:1; min-width:130px; border-right:1px solid #f3f4f6; }
.court-head { height:44px; display:flex; align-items:center; justify-content:center; font-size:0.72rem; font-weight:700; text-align:center; border-bottom:1px solid #e5e7eb; padding:0 4px; }
.court-body { position:relative; background:repeating-linear-gradient(to bottom, #fff, #fff 59px, #f3f4f6 59px, #f3f4f6 60px); }
.block { position:absolute; left:3px; right:3px; border-radius:6px; padding:3px 6px; overflow:hidden; font-size:0.65rem; }
.block.k-normal { background:#d3f1dd; border:1px solid #a7e3bc; }
.block.k-bot { background:#dbeafe; border:1px solid #93c5fd; }
.block.k-bot .bname { color:#1e3a8a; }
.block.k-futbol { background:#ffedd5; border:1px solid #fdba74; }
.block.k-futbol .bname { color:#9a3412; }
.block.k-clase { background:#ede9fe; border:1px solid #c4b5fd; }
.block.k-clase .bname { color:#5b21b6; }
.block.k-cerrado { background:#f3f4f6; border:1px solid #d1d5db; }
.block.k-cerrado .bname { color:#6b7280; }
.legend { display:flex; flex-wrap:wrap; gap:0.9rem; margin-bottom:0.6rem; font-size:0.7rem; color:#4b5563; }
.legend .sw { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:4px; vertical-align:-2px; }
.sw.k-normal { background:#d3f1dd; border:1px solid #a7e3bc; }
.sw.k-bot { background:#dbeafe; border:1px solid #93c5fd; }
.sw.k-futbol { background:#ffedd5; border:1px solid #fdba74; }
.sw.k-clase { background:#ede9fe; border:1px solid #c4b5fd; }
.sw.k-cerrado { background:#f3f4f6; border:1px solid #d1d5db; }
.bname { font-weight:700; color:#14532d; margin-top:2px; }
.btime { color:#4b5563; font-size:0.6rem; }
.pill { display:inline-block; font-size:0.56rem; font-weight:700; padding:1px 6px; border-radius:99px; }
.pill.unpaid { background:#dc2626; color:white; }
.pill.paid { background:#10b981; color:white; }
.pill.botp { background:#064e3b; color:#6ee7b7; }
.pill.clubp { background:#1e3a8a; color:#93c5fd; }
/* Tablas */
.tbl { width:100%; border-collapse:collapse; background:white; border-radius:12px; overflow:hidden; border:1px solid #e5e7eb; }
.tbl th { background:#0f172a; color:white; font-size:0.68rem; text-transform:uppercase; letter-spacing:0.04em; padding:0.6rem 0.9rem; text-align:left; }
.tbl td { padding:0.55rem 0.9rem; font-size:0.8rem; border-bottom:1px solid #f3f4f6; }
.dayhead { background:#ecfdf5; color:#047857; font-weight:800; font-size:0.72rem; }
.hora { font-weight:700; color:#d97706; white-space:nowrap; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:0.75rem; }
.card { background:white; border:1px solid #e5e7eb; border-radius:12px; padding:1rem; }
.card .num { font-size:1.7rem; font-weight:800; color:#10b981; }
.card .lbl { font-size:0.7rem; color:#6b7280; margin-top:0.2rem; }
.empty { background:white; border:1px solid #e5e7eb; border-radius:12px; padding:2rem; text-align:center; color:#6b7280; font-size:0.85rem; }
.hint { font-size:0.75rem; color:#6b7280; margin-top:0.75rem; }
.hint a { color:#059669; }
</style></head><body>
<div class="sidebar">
  <div class="brand">Match<span>Bot</span> Manager</div>
  <div class="nav-item active" data-s="calendario">📅 Calendario</div>
  <div class="nav-item" data-s="reservas">📋 Reservas</div>
  <div class="nav-item" data-s="clientes">👥 Clientes</div>
  <div class="nav-item" data-s="premios">🎟️ Premios</div>
  <div class="nav-item" data-s="pagos">💵 Pagos</div>
  <div class="nav-item" data-s="reportes">📊 Reportes</div>
  <a class="nav-item" href="https://manager.playtomic.io/dashboard/academy?tid=%TENANT%" target="_blank">🎓 Academia ↗</a>
</div>
<div class="main">
  <div class="topbar">
    <h1 id="title">📅 Calendario</h1>
    <div class="datenav">
      <a href="/panel?date=%PREV%">←</a>
      <span class="cur">%DATELABEL%</span>
      <a href="/panel?date=%NEXT%">→</a>
      <a href="/panel">Hoy</a>
    </div>
  </div>
  <section id="calendario" class="visible">%CALENDARIO%</section>
  <section id="reservas">%RESERVAS%</section>
  <section id="clientes">%CLIENTES%</section>
  <section id="premios">%PREMIOS%</section>
  <section id="pagos">%PAGOS%</section>
  <section id="reportes">%REPORTES%</section>
</div>
<script>
var titles = {calendario:'📅 Calendario', reservas:'📋 Reservas', clientes:'👥 Clientes',
              premios:'🎟️ Premios', pagos:'💵 Pagos', reportes:'📊 Reportes'};
document.querySelectorAll('.nav-item[data-s]').forEach(function(it){
  it.addEventListener('click', function(){
    document.querySelectorAll('.nav-item').forEach(function(n){ n.classList.remove('active'); });
    it.classList.add('active');
    var s = it.dataset.s;
    document.querySelectorAll('section').forEach(function(sec){ sec.classList.remove('visible'); });
    document.getElementById(s).classList.add('visible');
    document.getElementById('title').textContent = titles[s];
  });
});
</script>
</body></html>"""


async def render_panel(playtomic, date_str: str = None) -> str:
    now_local = _local(datetime.utcnow())
    try:
        day = date_cls.fromisoformat(date_str) if date_str else now_local.date()
    except ValueError:
        day = now_local.date()

    # Datos de Playtomic
    try:
        raw = await playtomic.list_matches()
    except Exception as e:
        logger.error(f"panel list_matches: {e}")
        raw = []

    all_rows = [r for r in (_match_row(m) for m in raw) if r]
    day_rows = [r for r in all_rows if r["start_l"].date() == day and "CANCEL" not in r["status"]]
    upcoming = sorted(
        [r for r in all_rows if r["start_l"].date() >= now_local.date() and "CANCEL" not in r["status"]],
        key=lambda r: r["start_l"],
    )

    try:
        await playtomic._ensure_resource_names()
        courts = list(playtomic._resource_names.values())
    except Exception:
        courts = sorted({r["court"] for r in day_rows})

    tenant = os.getenv("PLAYTOMIC_TENANT_ID", "")
    label = f"{DAYS[day.weekday()]} {day.day} {MONTHS[day.month]}"
    if day == now_local.date():
        label = "Hoy · " + label

    html = (PAGE
        .replace("%TENANT%", tenant)
        .replace("%PREV%", (day - timedelta(days=1)).isoformat())
        .replace("%NEXT%", (day + timedelta(days=1)).isoformat())
        .replace("%DATELABEL%", label)
        .replace("%CALENDARIO%", calendar_html(courts, day_rows, day))
        .replace("%RESERVAS%", reservas_html(upcoming))
        .replace("%CLIENTES%", clientes_html())
        .replace("%PREMIOS%", premios_html())
        .replace("%PAGOS%", pagos_html(upcoming))
        .replace("%REPORTES%", reportes_html())
    )
    return html
