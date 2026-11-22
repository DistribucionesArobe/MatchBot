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
        "id": str(m.get("match_id") or m.get("id") or ""),
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
                f'<div class="block k-{b.get("kind", "normal")}" '
                f'data-mid="{_esc(b.get("id", ""))}" data-dur="{b["dur"]}" '
                f'style="top:{top}px;height:{h}px">'
                f'{pay_pill}{bot_tag}'
                f'<div class="bname">{_esc(b["players"][:40])}</div>'
                f'<div class="btime">{_fmt_hhmm(b["start_l"])} - {_fmt_hhmm(end_l)}</div>'
                f'</div>'
            )
        cols += (
            f'<div class="court-col"><div class="court-head">{_esc(court)}</div>'
            f'<div class="court-body" data-court="{_esc(court)}" '
            f'style="height:{height}px">{blocks}</div></div>'
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
    # Base de clientes registrados + actividad de sellos (join por últimos 10 dígitos)
    try:
        rows = execute("""
            SELECT c.phone10, c.name, c.birthday, c.source,
                   COALESCE(c.bookings_count, 0) AS reservas,
                   c.last_booking_at AS ultima,
                   COALESCE(l.sellos, 0) AS sellos_total
            FROM customers c
            LEFT JOIN (
                SELECT RIGHT(wa_phone, 10) AS p10,
                       COUNT(*) FILTER (WHERE status='stamped') AS sellos
                FROM loyalty_bookings GROUP BY RIGHT(wa_phone, 10)
            ) l ON l.p10 = c.phone10
            ORDER BY reservas DESC, c.created_at DESC
            LIMIT 200
        """, fetch_all=True) or []
    except Exception as e:
        logger.error(f"panel clientes query: {e}")
        rows = []

    # Clientes con actividad de sellos que aún no se registran con nombre
    extra = []
    try:
        known = {r["phone10"] for r in rows}
        for r in execute("""
            SELECT RIGHT(wa_phone,10) AS p10, COUNT(*) AS reservas,
                   COUNT(*) FILTER (WHERE status='stamped') AS sellos,
                   MAX(created_at) AS ultima
            FROM loyalty_bookings GROUP BY RIGHT(wa_phone,10)
        """, fetch_all=True) or []:
            if r["p10"] not in known:
                extra.append(r)
    except Exception:
        pass

    if not rows and not extra:
        return '<div class="empty">Aún no hay clientes registrados. Se registran solos al hacer su primera reserva por el bot o por llamada.</div>'

    body = ""
    for r in rows:
        cycle = int(r["sellos_total"]) % 10
        fuente = "📞" if (r.get("source") == "llamada") else "💬"
        body += (
            f'<tr><td><b>{_esc(r["name"] or "Sin nombre")}</b> {fuente}</td>'
            f'<td>+52 {_esc(r["phone10"])}</td>'
            f'<td>{r["birthday"] or "—"}</td>'
            f'<td>{r["reservas"]}</td>'
            f'<td>🎾 {cycle}/10</td>'
            f'<td>{r["ultima"].strftime("%d/%m/%Y") if r["ultima"] else "—"}</td></tr>'
        )
    for r in extra:
        cycle = int(r["sellos"]) % 10
        body += (
            f'<tr><td><i>Sin registrar</i></td>'
            f'<td>+52 {_esc(r["p10"])}</td><td>—</td>'
            f'<td>{r["reservas"]}</td>'
            f'<td>🎾 {cycle}/10</td>'
            f'<td>{r["ultima"].strftime("%d/%m/%Y") if r["ultima"] else "—"}</td></tr>'
        )
    return (
        '<table class="tbl"><tr><th>Cliente</th><th>Teléfono</th><th>🎂 Cumple</th>'
        '<th>Reservas</th><th>Tarjeta</th><th>Última</th></tr>' + body + '</table>'
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


def monederos_html() -> str:
    """Sección Monederos dentro de Pagos: recargas + saldos."""
    try:
        from api import wallet
        packs = wallet.packages()
        balances = wallet.all_balances()
        txs = wallet.recent_tx(15)
    except Exception as e:
        logger.error(f"panel monederos: {e}")
        packs, balances, txs = [], [], []

    pack_btns = "".join(
        f'<button type="button" class="packbtn" '
        f'onclick="document.getElementById(\'w_paid\').value={p["paga"]:.0f};'
        f'document.getElementById(\'w_credit\').value={p["recibe"]:.0f}">'
        f'Paga ${p["paga"]:.0f} → recibe ${p["recibe"]:.0f}</button> '
        for p in packs)

    form = (
        '<div class="walletform"><h3>💰 Registrar recarga de monedero</h3>'
        '<form method="post" action="/api/wallet/recarga">'
        '<label>Teléfono del cliente (10 dígitos)<br>'
        '<input name="phone" required pattern="[0-9 +]{10,15}" placeholder="8341234567"></label> '
        f'<div style="margin:8px 0">{pack_btns}</div>'
        '<label>Pagó $<input id="w_paid" name="paid" type="number" step="1" required></label> '
        '<label>Abonar $<input id="w_credit" name="credit" type="number" step="1" required></label> '
        '<button type="submit" class="savebtn">Abonar saldo</button>'
        '</form><p class="hint">Al abonar, el cliente recibe aviso por WhatsApp y sus '
        'reservas se pagan solas con su saldo.</p></div>'
    )

    if balances:
        rows = "".join(
            f'<tr><td>+52 {_esc(b["phone10"])}</td>'
            f'<td><b>${float(b["saldo"]):.0f}</b></td>'
            f'<td>{b["ultima"].strftime("%d/%m/%Y") if b["ultima"] else "—"}</td></tr>'
            for b in balances)
        saldos = ('<h3>Saldos actuales</h3><table class="tbl">'
                  '<tr><th>Cliente</th><th>Saldo</th><th>Último mov.</th></tr>'
                  + rows + '</table>')
    else:
        saldos = '<div class="empty">Aún no hay monederos con saldo.</div>'

    if txs:
        rows = "".join(
            f'<tr><td>{t["created_at"].strftime("%d/%m %H:%M") if t["created_at"] else ""}</td>'
            f'<td>+52 {_esc(t["phone10"])}</td>'
            f'<td style="color:{"#0a7d3b" if float(t["amount"])>0 else "#b02a2a"}">'
            f'{"+" if float(t["amount"])>0 else ""}{float(t["amount"]):.0f}</td>'
            f'<td>{_esc(t.get("concept") or "")}</td></tr>'
            for t in txs)
        movs = ('<h3>Últimos movimientos</h3><table class="tbl">'
                '<tr><th>Fecha</th><th>Cliente</th><th>Monto</th><th>Concepto</th></tr>'
                + rows + '</table>')
    else:
        movs = ''

    return form + saldos + movs


def pagos_html(upcoming: list[dict]) -> str:
    wallet_block = monederos_html()
    if not upcoming:
        return wallet_block + '<div class="empty">No hay reservas próximas.</div>'
    body = ""
    for b in upcoming:
        estado = ('<span class="pill paid">Pagado</span>' if b["paid"]
                  else '<span class="pill unpaid">Por cobrar</span>')
        body += (
            f'<tr><td>{DAYS[b["start_l"].weekday()][:3]} {b["start_l"].day}/{b["start_l"].month} · {_fmt_hhmm(b["start_l"])}</td>'
            f'<td>{_esc(b["court"])}</td><td>{_esc(b["players"][:35])}</td>'
            f'<td><b>{b["price"]}</b></td><td>{estado}</td></tr>'
        )
    return wallet_block + (
        '<h3>Reservas próximas</h3>'
        '<table class="tbl"><tr><th>Cuándo</th><th>Cancha</th>'
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
.walletform { background:white; border:1px solid #e5e7eb; border-radius:12px; padding:1.2rem; margin-bottom:1rem; }
.walletform h3, #pagos h3 { margin:0.4rem 0 0.8rem; font-size:0.95rem; }
.walletform label { font-size:0.8rem; color:#374151; margin-right:0.8rem; }
.walletform input { border:1px solid #d1d5db; border-radius:8px; padding:6px 8px; margin:2px 6px 8px 0; width:130px; }
.packbtn { background:#eef2ff; border:1px solid #c7d2fe; color:#3730a3; border-radius:8px; padding:5px 10px; font-size:0.78rem; cursor:pointer; margin-bottom:4px; }
.savebtn { background:#111827; color:white; border:none; border-radius:8px; padding:8px 16px; cursor:pointer; }
.hint { color:#6b7280; font-size:0.75rem; margin:0.5rem 0 0; }
.hint { font-size:0.75rem; color:#6b7280; margin-top:0.75rem; }
.hint a { color:#059669; }
.userchip { margin-top:auto; padding:0.8rem 1rem; font-size:0.75rem; color:#9ca3af; border-top:1px solid #1f2937; }
.userchip a { color:#f87171; text-decoration:none; margin-left:6px; }
.userform { background:white; border:1px solid #e5e7eb; border-radius:12px; padding:1.2rem; margin-bottom:1rem; }
.userform label { font-size:0.8rem; color:#374151; margin-right:0.8rem; }
.userform input, .userform select { border:1px solid #d1d5db; border-radius:8px; padding:6px 8px; margin:2px 6px 8px 0; }
.modal-bg { display:none; position:fixed; inset:0; background:rgba(17,24,39,.45); z-index:50; align-items:center; justify-content:center; }
.modal { background:white; border-radius:16px; padding:1.6rem; width:330px; box-shadow:0 20px 50px rgba(0,0,0,.2); }
.modal h3 { margin:0 0 0.2rem; }
.modal-sub { color:#6b7280; font-size:0.85rem; margin:0 0 1rem; }
.modal label { display:block; font-size:0.8rem; color:#374151; margin-bottom:0.7rem; }
.modal input, .modal select { width:100%; border:1px solid #d1d5db; border-radius:8px; padding:8px; margin-top:3px; box-sizing:border-box; }
.modal-btns { display:flex; gap:8px; justify-content:flex-end; margin-top:0.8rem; }
.cancelbtn { background:#f3f4f6; border:1px solid #d1d5db; border-radius:8px; padding:8px 14px; cursor:pointer; }
.block[data-mid] { cursor:grab; }
.block.dragging { opacity:0.75; box-shadow:0 10px 24px rgba(0,0,0,.3); z-index:30; cursor:grabbing; }
.toast { position:fixed; bottom:22px; right:22px; background:#111827; color:white; padding:10px 16px;
         border-radius:10px; font-size:0.85rem; z-index:99; box-shadow:0 8px 24px rgba(0,0,0,.25); }
.toast.bad { background:#b02a2a; }
.configform { background:white; border:1px solid #e5e7eb; border-radius:12px; padding:1.2rem; margin-bottom:1rem; }
.configform label { display:inline-block; font-size:0.8rem; color:#374151; margin:0 1rem 0.7rem 0; }
.configform input { display:block; border:1px solid #d1d5db; border-radius:8px; padding:7px 9px; margin-top:3px; width:150px; }
.configform input.wide { width:330px; }
.configform h4 { margin:0.8rem 0 0.5rem; font-size:0.85rem; color:#111827; }
</style></head><body>
<div class="sidebar">
  <div class="brand">Match<span>Bot</span> Manager</div>
  <div class="nav-item active" data-s="calendario">📅 Calendario</div>
  <div class="nav-item" data-s="reservas">📋 Reservas</div>
  <div class="nav-item" data-s="clientes">👥 Clientes</div>
  <div class="nav-item" data-s="premios">🎟️ Premios</div>
  <div class="nav-item" data-s="pagos">💵 Pagos</div>
  <div class="nav-item" data-s="reportes">📊 Reportes</div>
  %NAV_USUARIOS%
  <div class="userchip">%USERCHIP%</div>
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
  <section id="usuarios">%USUARIOS%</section>
  <section id="config">%CONFIG%</section>
</div>

<div id="bookmodal" class="modal-bg">
  <form class="modal" method="post" action="/api/panel/book">
    <h3>➕ Nueva reserva</h3>
    <p class="modal-sub" id="bm_court_label"></p>
    <input type="hidden" name="court" id="bm_court">
    <input type="hidden" name="date" value="%DAYISO%">
    <label>Hora de inicio<br><input type="time" name="time" id="bm_time" step="1800" required></label>
    <label>Duración<br><select name="duration" id="bm_dur">
      <option value="60">60 minutos</option>
      <option value="90">90 minutos</option>
      <option value="120">120 minutos</option>
    </select></label>
    <label>Nombre del cliente<br><input name="name" id="bm_name" required placeholder="Juan Pérez"></label>
    <label>Teléfono (opcional, para avisos y sellos)<br><input name="phone" placeholder="8341234567"></label>
    <label>Cobro<br><select name="cobro" id="bm_cobro">
      <option value="single">Pago único (uno paga todo)</option>
      <option value="split">Dividido entre jugadores</option>
    </select></label>
    <label id="bm_players_wrap" style="display:none">¿Entre cuántos se divide?<br>
      <input type="number" name="players" id="bm_players" min="1" max="14" value="4"></label>
    <div class="modal-btns">
      <button type="button" class="cancelbtn" onclick="closeBookModal()">Cancelar</button>
      <button type="submit" class="savebtn">Crear reserva</button>
    </div>
  </form>
</div>

<script>
var titles = {calendario:'📅 Calendario', reservas:'📋 Reservas', clientes:'👥 Clientes',
              premios:'🎟️ Premios', pagos:'💵 Pagos', reportes:'📊 Reportes',
              usuarios:'👤 Usuarios', config:'⚙️ Configuración'};
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

// ── Crear reserva picando el calendario ──
var PX_MIN = %PXMIN%, GRID_START = %GRIDSTART%, DAYISO = '%DAYISO%';
function closeBookModal(){ document.getElementById('bookmodal').style.display='none'; }
document.getElementById('bookmodal').addEventListener('click', function(e){
  if (e.target === this) closeBookModal();
});
function toast(msg, bad){
  var t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function(){ t.remove(); }, 3500);
}
// Enviar sin salir de la página: el cuadro aparece al recargar el calendario
document.querySelector('#bookmodal form').addEventListener('submit', function(e){
  e.preventDefault();
  var f = e.target;
  var btn = f.querySelector('.savebtn');
  btn.disabled = true; btn.textContent = 'Creando...';
  fetch('/api/panel/book?ajax=1', {method:'POST', body:new FormData(f)})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.ok) {
        toast('✅ Reserva creada para ' + d.name);
        setTimeout(function(){ location.href = '/panel?date=' + DAYISO; }, 700);
      } else {
        toast('😕 ' + (d.error || 'No se pudo crear'), true);
        btn.disabled = false; btn.textContent = 'Crear reserva';
      }
    })
    .catch(function(){ toast('😕 Error de red', true);
      btn.disabled = false; btn.textContent = 'Crear reserva'; });
});

// ── Arrastrar una reserva para moverla de hora/cancha ──
(function(){
  var drag = null;
  function minsToHHMM(mins){
    var hh = String(Math.floor(mins/60)).padStart(2,'0');
    return hh + ':' + String(mins%60).padStart(2,'0');
  }
  document.querySelectorAll('.block[data-mid]').forEach(function(bl){
    if (!bl.dataset.mid) return;
    bl.addEventListener('mousedown', function(e){
      e.preventDefault();
      drag = { bl: bl, startY: e.clientY, startX: e.clientX,
               origBody: bl.parentElement, origTop: parseInt(bl.style.top),
               moved: false };
    });
  });
  document.addEventListener('mousemove', function(e){
    if (!drag) return;
    if (!drag.moved && Math.abs(e.clientY-drag.startY)+Math.abs(e.clientX-drag.startX) < 6) return;
    drag.moved = true;
    drag.bl.classList.add('dragging');
    drag.bl.style.pointerEvents = 'none';
    var under = document.elementFromPoint(e.clientX, e.clientY);
    var body = under && under.closest ? under.closest('.court-body') : null;
    if (body) {
      if (drag.bl.parentElement !== body) body.appendChild(drag.bl);
      var rect = body.getBoundingClientRect();
      var mins = GRID_START + (e.clientY - rect.top) / PX_MIN;
      mins = Math.round(mins/30)*30;
      drag.curMins = mins;
      drag.curBody = body;
      drag.bl.style.top = ((mins - GRID_START) * PX_MIN) + 'px';
    }
  });
  document.addEventListener('mouseup', function(e){
    if (!drag) return;
    var d = drag; drag = null;
    d.bl.classList.remove('dragging');
    d.bl.style.pointerEvents = '';
    if (!d.moved || !d.curBody) return;
    var court = d.curBody.dataset.court;
    var hhmm = minsToHHMM(d.curMins);
    if (!confirm('¿Mover esta reserva a ' + court + ' a las ' + hhmm + '?')) {
      location.reload(); return;
    }
    toast('Moviendo reserva...');
    var fd = new FormData();
    fd.append('match_id', d.bl.dataset.mid);
    fd.append('court', court);
    fd.append('date', DAYISO);
    fd.append('time', hhmm);
    fd.append('duration', d.bl.dataset.dur || '90');
    fetch('/api/panel/move', {method:'POST', body:fd})
      .then(function(r){ return r.json(); })
      .then(function(r2){
        if (r2.ok) { toast('✅ Reserva movida'); setTimeout(function(){ location.reload(); }, 700); }
        else { toast('😕 ' + (r2.error || 'No se pudo mover'), true);
               setTimeout(function(){ location.reload(); }, 1800); }
      })
      .catch(function(){ toast('😕 Error de red', true);
                         setTimeout(function(){ location.reload(); }, 1800); });
  });
})();
document.getElementById('bm_cobro').addEventListener('change', function(){
  document.getElementById('bm_players_wrap').style.display =
    this.value === 'split' ? 'block' : 'none';
});
document.querySelectorAll('.court-body').forEach(function(body){
  body.style.cursor = 'crosshair';
  body.addEventListener('click', function(e){
    if (e.target.closest('.block')) return;  // ya hay reserva ahí
    var rect = body.getBoundingClientRect();
    var mins = GRID_START + (e.clientY - rect.top) / PX_MIN;
    mins = Math.round(mins / 30) * 30;
    var hh = String(Math.floor(mins / 60)).padStart(2, '0');
    var mm = String(mins % 60).padStart(2, '0');
    var court = body.dataset.court || '';
    var isFut = /f[uú]t?bol/i.test(court);
    document.getElementById('bm_court').value = court;
    document.getElementById('bm_court_label').textContent = '🎾 ' + court;
    document.getElementById('bm_time').value = hh + ':' + mm;
    document.getElementById('bm_dur').value = isFut ? '60' : '90';
    document.getElementById('bm_players').value = isFut ? 7 : 4;
    document.getElementById('bookmodal').style.display = 'flex';
    document.getElementById('bm_name').focus();
  });
});
</script>
</body></html>"""


def config_html() -> str:
    """⚙️ Configuración (solo superadmin): horarios, precios, monedero, teléfonos."""
    try:
        from api import settings as st
        v = st.all_values()
    except Exception as e:
        logger.error(f"panel config: {e}")
        return '<div class="empty">No se pudo cargar la configuración.</div>'

    def inp(key, tipo="text", wide=False):
        cls = ' class="wide"' if wide else ""
        return (f'<label>{st.LABELS.get(key, key)}'
                f'<input{cls} type="{tipo}" name="{key}" value="{_esc(v.get(key, ""))}"></label>')

    return (
        '<form class="configform" method="post" action="/api/panel/settings" '
        'onsubmit="this.querySelector(\'button\').disabled=true">'
        '<h3>⚙️ Configuración del club</h3>'
        '<p class="hint">Estos valores los usa el bot de WhatsApp, la recepcionista '
        'de voz y este panel. Los cambios aplican en ~1 minuto.</p>'
        '<h4>🕐 Horarios (los usa el bot para ofrecer canchas)</h4>'
        + inp("h_lv_abre", "time") + inp("h_lv_cierra", "time")
        + inp("h_sab_abre", "time") + inp("h_sab_cierra", "time")
        + inp("h_dom_abre", "time") + inp("h_dom_cierra", "time")
        + '<h4>💲 Precios</h4>'
        + inp("precio_respaldo", "number") + inp("precio_clase", "number")
        + '<h4>💰 Monedero</h4>'
        + inp("paquetes_monedero", "text", wide=True)
        + '<h4>📱 Teléfonos de aviso</h4>'
        + inp("tel_avisos") + inp("tel_clases")
        + '<div style="margin-top:0.8rem"><button type="submit" class="savebtn">'
          'Guardar configuración</button></div>'
        '</form>'
    )


def usuarios_html() -> str:
    """Sección Usuarios (solo superadmin): crear staff y ver cuentas."""
    try:
        from api import auth
        users = auth.list_users()
    except Exception as e:
        logger.error(f"panel usuarios: {e}")
        users = []
    form = (
        '<div class="userform"><h3>➕ Crear o actualizar usuario</h3>'
        '<form method="post" action="/api/panel/users">'
        '<label>Usuario<input name="username" required placeholder="caro"></label>'
        '<label>Nombre<input name="name" required placeholder="Caro García"></label>'
        '<label>Contraseña<input name="password" type="password" required></label>'
        '<label>Rol<select name="role">'
        '<option value="staff">Staff (ver + recargas)</option>'
        '<option value="superadmin">Super admin</option></select></label> '
        '<button type="submit" class="savebtn">Guardar</button>'
        '</form><p class="hint">Si el usuario ya existe, se actualiza su contraseña y rol. '
        'Para desactivar: agrega ?off=usuario a /api/panel/users</p></div>'
    )
    rows = "".join(
        f'<tr><td><b>{_esc(u["name"] or u["username"])}</b></td>'
        f'<td>{_esc(u["username"])}</td>'
        f'<td>{"👑 Super admin" if u["role"] == "superadmin" else "👔 Staff"}</td>'
        f'<td>{"✅ Activo" if u["active"] else "⛔ Inactivo"}</td></tr>'
        for u in users)
    tabla = ('<table class="tbl"><tr><th>Nombre</th><th>Usuario</th>'
             '<th>Rol</th><th>Estado</th></tr>' + rows + '</table>') if users else ''
    return form + tabla


async def render_panel(playtomic, date_str: str = None, user: dict = None) -> str:
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

    is_super = bool(user and user.get("role") == "superadmin")
    nav_usuarios = ('<div class="nav-item" data-s="usuarios">👤 Usuarios</div>'
                    '<div class="nav-item" data-s="config">⚙️ Configuración</div>'
                    if is_super else "")
    userchip = ""
    if user:
        rol = "👑" if is_super else "👔"
        userchip = (f'{rol} {_esc(user.get("name") or user.get("sub") or "")} '
                    f'<a href="/panel/logout">Salir</a>')

    html = (PAGE
        .replace("%PXMIN%", str(PX_PER_MIN))
        .replace("%GRIDSTART%", str(GRID_START_MIN))
        .replace("%DAYISO%", day.isoformat())
        .replace("%NAV_USUARIOS%", nav_usuarios)
        .replace("%USERCHIP%", userchip)
        .replace("%USUARIOS%", usuarios_html() if is_super else "")
        .replace("%CONFIG%", config_html() if is_super else "")
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
