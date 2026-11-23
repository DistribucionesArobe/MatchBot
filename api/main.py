"""
MatchBot - Main FastAPI Application
https://matchbot.live
"""
import hashlib
import hmac
import json
import logging
import asyncio
from datetime import date, datetime
from typing import Optional
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

from config.settings import settings
from db.database import init_db, execute
from api.availability import get_available_slots, get_slots_summary
from api.bookings import (
    create_booking, confirm_booking, cancel_booking,
    get_bookings_for_date, get_customer_bookings, BookingError
)
from whatsapp.booking_flow import handle_message
from api.playtomic_client import playtomic

# ─── Logging ───
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("matchbot")

# ─── App ───
app = FastAPI(title="MatchBot", version="1.0.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Message dedup & buffering (from CotizaExpress pattern) ───
_processed_msgs: dict[str, float] = {}
_msg_buffers: dict[str, list] = defaultdict(list)
_buffer_tasks: dict[str, asyncio.Task] = {}
MSG_DEDUP_WINDOW = 120  # seconds


# ─────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    try:
        init_db()
        logger.info("🎾 MatchBot started — DB connected")
    except Exception as e:
        logger.warning(f"⚠️ DB not available: {e} — running without database")

    # Pre-load Playtomic court names at startup
    try:
        await playtomic._ensure_resource_names()
        logger.info(f"Playtomic court names loaded: {list(playtomic._resource_names.values())}")
    except Exception as e:
        logger.warning(f"Could not pre-load court names: {e}")

    # Daily self-check with WhatsApp alert on failure
    asyncio.create_task(_daily_health_loop())

    # Loyalty card: create tables + hourly stamp processing
    try:
        from api import loyalty
        loyalty.ensure_tables()
        logger.info("🎟️ Loyalty tables ready")
        from api import promos
        promos.ensure_tables()
        from api import customers
        customers.ensure_table()
        logger.info("📇 Customers table ready")
        from api import wallet
        wallet.ensure_tables()
        logger.info("💰 Wallet tables ready")
        from api import auth
        auth.ensure_table()
        auth.bootstrap_superadmin()
        logger.info("🔐 Panel auth ready")
        from api import settings as app_settings
        app_settings.ensure_table()
        logger.info("⚙️ Settings table ready")
        from api import mirror
        mirror.ensure_table()
        logger.info("🪞 Mirror table ready")
        from api import tournaments
        tournaments.ensure_table()
        logger.info("🏆 Tournaments table ready")
    except Exception as e:
        logger.warning(f"Loyalty tables init failed: {e}")
    asyncio.create_task(_loyalty_loop())
    asyncio.create_task(_birthday_loop())
    asyncio.create_task(_mirror_loop())

    logger.info("🎾 MatchBot started — matchbot.live")


# ─────────────────────────────────────────────────────
# LOYALTY — hourly stamp confirmation + reward notices
# ─────────────────────────────────────────────────────

async def _loyalty_loop():
    """Every hour: confirm stamps for played bookings and notify
    customers (and the owner) when a reward is unlocked."""
    from api import loyalty
    from whatsapp.sender import send_text
    await asyncio.sleep(300)  # wait for boot
    while True:
        try:
            events = await loyalty.process_pending(playtomic)
            phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
            token = os.getenv("WHATSAPP_TOKEN", "")
            notify_owner = os.getenv("CLUB_NOTIFY_PHONE", "528342546466")
            if phone_id and token:
                for ev in events:
                    try:
                        if ev["event"] == "half":
                            await send_text(phone_id, token, ev["phone"],
                                "🎉 *¡Llegaste a 5 sellos!*\n\n"
                                "Tu siguiente renta es a *MITAD DE PRECIO* 🎾\n"
                                "Se aplica automáticamente en tu próxima reserva.\n\n"
                                "Escribe *Reservar* para usarla.")
                            await send_text(phone_id, token, notify_owner,
                                f"🎟️ Cliente +{ev['phone']} llegó a 5 sellos — su próxima renta va a MITAD DE PRECIO.")
                        elif ev["event"] == "free":
                            await send_text(phone_id, token, ev["phone"],
                                "🏆 *¡COMPLETASTE TU TARJETA — 10 SELLOS!*\n\n"
                                "Tu siguiente renta es *GRATIS* 🎾🎉\n"
                                "Se aplica automáticamente en tu próxima reserva "
                                "y tu tarjeta empieza de nuevo.\n\n"
                                "Escribe *Reservar* para usarla.")
                            await send_text(phone_id, token, notify_owner,
                                f"🏆 Cliente +{ev['phone']} completó 10 sellos — su próxima renta es GRATIS.")
                        elif ev["event"] == "stamp":
                            await send_text(phone_id, token, ev["phone"],
                                f"🎟️ ¡Sello ganado por tu partido! Llevas *{ev['stamps']}/10*.\n"
                                f"Al sello 5: mitad de precio · Al 10: cancha gratis.\n"
                                f"Escribe *sellos* para ver tu tarjeta.")
                    except Exception as e:
                        logger.error(f"Loyalty notification failed: {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Loyalty loop error: {e}")
        await asyncio.sleep(3600)  # every hour


# ─────────────────────────────────────────────────────
# ESPEJO LOCAL — copia de todas las reservas (cada 30 min)
# ─────────────────────────────────────────────────────

async def _mirror_loop():
    """Sincroniza el espejo local con Playtomic cada 30 minutos, para que
    MatchBot tenga copia completa de TODAS las reservas (incluidas las
    que el staff crea en el Manager)."""
    from api import mirror
    await asyncio.sleep(420)
    while True:
        try:
            await mirror.sync(playtomic)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Mirror loop error: {e}")
        await asyncio.sleep(1800)


# ─────────────────────────────────────────────────────
# CUMPLEAÑOS — felicitación automática (plantilla promo)
# ─────────────────────────────────────────────────────

async def _birthday_loop():
    """Cada hora revisa si es ~11 AM local y felicita a los clientes
    que cumplen años hoy (una vez por año, respeta las bajas de promos)."""
    from api import customers, promos
    from whatsapp.sender import send_template, send_text
    await asyncio.sleep(600)
    offset = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
    while True:
        try:
            from datetime import datetime as _dt, timedelta as _td
            local = _dt.utcnow() + _td(hours=offset)
            if 11 <= local.hour < 12:
                cumpleaneros = customers.today_birthdays()
                if cumpleaneros:
                    phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
                    token = os.getenv("WHATSAPP_TOKEN", "")
                    notify_owner = os.getenv("CLUB_NOTIFY_PHONE", "528342546466")
                    optout = promos.opted_out_set()
                    for c in cumpleaneros:
                        to = c.get("wa_phone") or ("52" + c["phone10"])
                        if c["phone10"] in {p[-10:] for p in optout}:
                            customers.mark_bday_greeted(c["phone10"])
                            continue
                        first = (c.get("name") or "").split()[0]
                        msg = (f"🎂 ¡Feliz cumpleaños, {first}! 🎉 Todo el equipo del club "
                               f"te desea un gran día. Ven a celebrarlo a la cancha 🎾")
                        try:
                            await send_template(phone_id, token, to,
                                                promos.TEMPLATE_NAME, [msg],
                                                promos.TEMPLATE_LANG)
                            customers.mark_bday_greeted(c["phone10"])
                            await send_text(phone_id, token, notify_owner,
                                f"🎂 Hoy cumple años *{c.get('name')}* (+52 {c['phone10']}). "
                                f"Ya le mandé felicitación. ¡Échale porras si viene!")
                        except Exception as e:
                            logger.error(f"birthday greet failed for {c['phone10']}: {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Birthday loop error: {e}")
        await asyncio.sleep(3600)


# ─────────────────────────────────────────────────────
# DAILY HEALTH CHECK — alerts club owner via WhatsApp
# ─────────────────────────────────────────────────────

async def _run_health_check() -> list[str]:
    """Verify the Playtomic integration end-to-end (without creating
    bookings). Returns a list of problems (empty = all good)."""
    problems = []
    try:
        # 1. Fresh login
        ok = await playtomic.login()
        if not ok:
            problems.append("Login a Playtomic falló")
            return problems

        # 2. Fresh tenant token with write permissions
        playtomic.tenant_token = None
        await playtomic.get_tenant_token()
        claims = getattr(playtomic, "_tenant_token_claims", {})
        scopes = claims.get("scopes", []) or []
        if "UP_MATCHES_RW" not in [str(s) for s in scopes]:
            problems.append("El token NO tiene permiso para crear reservas (UP_MATCHES_RW)")

        # 3. Availability for tomorrow
        from datetime import timedelta as _td
        offset = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
        local_now = datetime.utcnow() + _td(hours=offset)
        tomorrow = (local_now + _td(days=1)).strftime("%Y-%m-%d")
        avail = await playtomic.get_availability(tomorrow)
        if not avail:
            problems.append(f"Disponibilidad vacía para mañana ({tomorrow})")
    except Exception as e:
        problems.append(f"Error inesperado: {str(e)[:150]}")
    return problems


async def _send_health_alert(problems: list[str]):
    """Send WhatsApp alert to the club owner."""
    from whatsapp.sender import send_text
    phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
    token = os.getenv("WHATSAPP_TOKEN", "")
    notify = os.getenv("CLUB_NOTIFY_PHONE", "528342546466")
    if not phone_id or not token:
        logger.error("Health alert: WhatsApp credentials not configured")
        return
    msg = (
        "⚠️ *MatchBot — Alerta del chequeo automático*\n\n"
        + "\n".join(f"• {p}" for p in problems)
        + "\n\nEl bot podría no estar funcionando bien. "
        "Los clientes verán un mensaje de error si intentan reservar."
    )
    # send_alert: texto normal, y si la ventana de 24h está cerrada,
    # usa la plantilla aviso_club (se entrega siempre)
    from api.promos import send_alert
    await send_alert(phone_id, token, notify, msg)


_health_was_ok = True  # avoid repeated alerts; notify on state change only


async def _daily_health_loop():
    """Run the health check every 4 hours. Alert on failure, and send a
    recovery message when the problem clears. Only alerts on STATE
    CHANGES (no spam every 4h while broken)."""
    global _health_was_ok
    # Small delay after boot so deploys don't trigger noise
    await asyncio.sleep(600)
    while True:
        try:
            problems = await _run_health_check()
            if problems:
                logger.warning(f"Health check FAILED: {problems}")
                if _health_was_ok:
                    await _send_health_alert(problems)
                    _health_was_ok = False
            else:
                logger.info("Health check OK")
                if not _health_was_ok:
                    _health_was_ok = True
                    try:
                        from whatsapp.sender import send_text
                        phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
                        token = os.getenv("WHATSAPP_TOKEN", "")
                        notify = os.getenv("CLUB_NOTIFY_PHONE", "528342546466")
                        if phone_id and token:
                            from api.promos import send_alert as _sa
                            await _sa(phone_id, token, notify,
                                "✅ *MatchBot se recuperó* — el sistema de reservas vuelve a funcionar con normalidad.")
                    except Exception as e:
                        logger.error(f"Recovery notice failed: {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Health loop error: {e}")
        await asyncio.sleep(4 * 3600)  # every 4 hours


@app.get("/api/voice/disponibilidad")
async def api_voice_disponibilidad(request: Request, date: str = Query("hoy")):
    """Herramienta del agente de voz: horarios libres."""
    from api import voice
    if not voice._auth_ok(request):
        raise HTTPException(403, "X-Voice-Key invalida o no configurada")
    return await voice.disponibilidad(playtomic, date)


@app.post("/api/voice/reservar")
async def api_voice_reservar(request: Request):
    """Herramienta del agente de voz: crear reserva."""
    from api import voice
    if not voice._auth_ok(request):
        raise HTTPException(403, "X-Voice-Key invalida o no configurada")
    body = await request.json()
    return await voice.reservar(
        playtomic,
        body.get("date", ""), body.get("time", ""),
        body.get("nombre", ""), body.get("telefono", ""),
    )


@app.post("/api/wallet/recarga")
async def api_wallet_recarga(request: Request):
    """Registrar recarga de monedero desde /panel (form). Requiere clave staff."""
    from fastapi.responses import HTMLResponse
    from api import wallet, customers
    from api import auth
    form = await request.form()
    user = auth.current_user(request)
    if not user:
        # Fallback para uso por API (curl): clave staff
        key = (form.get("key") or "").strip()
        staff_key = os.getenv("STAFF_KEY", "") or os.getenv("VOICE_API_KEY", "")
        if not staff_key or key != staff_key:
            return HTMLResponse(
                "<h3>❌ Inicia sesión en el panel para registrar recargas.</h3>"
                "<a href='/panel/login'>→ Iniciar sesión</a>", status_code=403)
    phone = (form.get("phone") or "").strip()
    try:
        paid = float(form.get("paid") or 0)
        credited = float(form.get("credit") or 0)
    except ValueError:
        paid, credited = 0.0, 0.0
    p10 = wallet._phone10(phone)
    if len(p10) != 10 or credited <= 0:
        return HTMLResponse(
            "<h3>❌ Revisa el teléfono (10 dígitos) y el monto a abonar.</h3>"
            "<a href='/panel'>← Volver al panel</a>", status_code=400)

    new_balance = wallet.credit(p10, credited, paid,
                                concept=f"Recarga (pagó ${paid:.0f})",
                                created_by=(user or {}).get("sub", "api"))

    # Avisar al cliente por WhatsApp
    try:
        from api.promos import send_alert
        phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
        token = os.getenv("WHATSAPP_TOKEN", "")
        nombre = customers.registered_name(p10)
        saludo = f"¡Hola{', ' + nombre.split()[0] if nombre else ''}!"
        if phone_id and token:
            await send_alert(phone_id, token, "52" + p10,
                f"💰 {saludo} Tu monedero del club fue recargado.\n\n"
                f"➕ Abono: ${credited:.0f} MXN\n"
                f"💵 Saldo actual: *${new_balance:.0f} MXN*\n\n"
                f"Tus próximas reservas se pagan automáticamente con tu saldo. "
                f"Escribe *saldo* para consultarlo. 🎾")
    except Exception as e:
        logger.error(f"wallet recarga notify failed: {e}")

    return HTMLResponse(
        f"<h3>✅ Recarga registrada: +${credited:.0f} a +52 {p10}.</h3>"
        f"<p>Saldo actual: <b>${new_balance:.0f} MXN</b>. "
        f"El cliente ya recibió aviso por WhatsApp.</p>"
        f"<a href='/panel'>← Volver al panel</a>")


@app.get("/api/voice/calling-settings")
async def api_voice_calling_settings(request: Request, creds: int = Query(0)):
    """Ver la config de llamadas (Calling API) del numero del bot."""
    from api import voice
    import httpx as _hx
    if not voice._auth_ok(request):
        raise HTTPException(403, "X-Voice-Key invalida o no configurada")
    token = os.getenv("WHATSAPP_TOKEN", "")
    phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
    url = f"https://graph.facebook.com/v25.0/{phone_id}/settings"
    if creds:
        url += "?include_sip_credentials=true"
    async with _hx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return {"status_code": r.status_code, "settings": data}


@app.post("/api/voice/enable-whatsapp-calling")
async def api_voice_enable_whatsapp_calling(request: Request):
    """Activa llamadas de WhatsApp en el numero del bot y las enruta por SIP
    al asistente de voz de Telnyx. NO afecta los mensajes del bot.
    Revertible con /api/voice/disable-whatsapp-calling."""
    from api import voice
    import httpx as _hx
    if not voice._auth_ok(request):
        raise HTTPException(403, "X-Voice-Key invalida o no configurada")
    token = os.getenv("WHATSAPP_TOKEN", "")
    phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
    sip_host = os.getenv(
        "VOICE_SIP_HOSTNAME",
        "assistant-d7187978-e40f-45ed-a59e-2a80de3d9e43.sip.telnyx.com")
    sip_port = int(os.getenv("VOICE_SIP_PORT", "5061"))
    payload = {
        "calling": {
            "status": "ENABLED",
            "call_icon_visibility": "DEFAULT",
            "sip": {
                "status": "ENABLED",
                "servers": [{"hostname": sip_host, "port": sip_port}],
            },
        }
    }
    async with _hx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://graph.facebook.com/v25.0/{phone_id}/settings",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=payload)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return {"status_code": r.status_code, "result": data,
            "sip_host": sip_host, "sip_port": sip_port}


@app.post("/api/voice/disable-whatsapp-calling")
async def api_voice_disable_whatsapp_calling(request: Request):
    """Desactiva las llamadas de WhatsApp del numero del bot (reversa total)."""
    from api import voice
    import httpx as _hx
    if not voice._auth_ok(request):
        raise HTTPException(403, "X-Voice-Key invalida o no configurada")
    token = os.getenv("WHATSAPP_TOKEN", "")
    phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
    payload = {"calling": {"status": "DISABLED",
                           "sip": {"status": "DISABLED", "servers": []}}}
    async with _hx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://graph.facebook.com/v25.0/{phone_id}/settings",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=payload)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return {"status_code": r.status_code, "result": data}


@app.get("/api/find-waba")
async def api_find_waba(candidates: str = Query("")):
    """Busca en que WABA vive el numero del bot (PHONE_NUMBER_ID_PADEL)."""
    import httpx as _hx
    token = os.getenv("WHATSAPP_TOKEN", "")
    target_phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
    results = {"target_phone_id": target_phone_id, "checked": {}}
    ids = [c.strip() for c in candidates.split(",") if c.strip()]
    if not ids:
        # WABAs vistos en el Business Manager del usuario (oct 2026)
        ids = ["4498523450386715", "2498253880588976", "1651710629422822",
               "1433446114449373", "1224835083125902", "2463184987468277"]
    async with _hx.AsyncClient(timeout=20) as client:
        for waba in ids:
            try:
                r = await client.get(
                    f"https://graph.facebook.com/v25.0/{waba}/phone_numbers",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code == 200:
                    nums = r.json().get("data", [])
                    results["checked"][waba] = [
                        {"id": n.get("id"), "display": n.get("display_phone_number"),
                         "name": n.get("verified_name")} for n in nums
                    ]
                    if any(str(n.get("id")) == target_phone_id for n in nums):
                        results["FOUND"] = waba
                else:
                    results["checked"][waba] = f"{r.status_code}: {r.text[:120]}"
            except Exception as e:
                results["checked"][waba] = f"error: {str(e)[:100]}"
    return results


@app.get("/api/probe-availability")
async def api_probe_availability(date: str = Query("2026-11-06")):
    """TEMP: prueba variantes del endpoint de disponibilidad para
    descubrir el formato nuevo tras ENDPOINT_NOT_ALLOWED."""
    import httpx as _hx
    from api.playtomic_client import TENANT_ID as _TID, PLAYTOMIC_API as _API
    await playtomic.ensure_tenant_auth()
    tok = playtomic.tenant_token or playtomic.token
    mgr_headers = {
        "Authorization": f"Bearer {tok}",
        "x-requested-with": "com.playtomic.manager 1.299.0+build.5655",
        "x-authorization-scope": f"tenant:{_TID}",
    }
    plain = {"Authorization": f"Bearer {tok}"}
    base_params = {
        "sport_id": "PADEL",
        "tenant_id": _TID,
        "local_start_min": f"{date}T00:00:00",
        "local_start_max": f"{date}T23:59:59",
    }
    variants = {
        "v1 + user_id=me + plain": (f"{_API}/v1/availability", {**base_params, "user_id": "me"}, plain),
        "v1 + user_id=me + mgr_headers": (f"{_API}/v1/availability", {**base_params, "user_id": "me"}, mgr_headers),
        "v1 sin user_id + mgr_headers": (f"{_API}/v1/availability", base_params, mgr_headers),
        "v1 sin user_id + plain": (f"{_API}/v1/availability", base_params, plain),
        "v2 + mgr_headers": (f"{_API}/v2/availability", {**base_params, "user_id": "me"}, mgr_headers),
    }
    out = {}
    for label, (url, params, headers) in variants.items():
        try:
            r = await playtomic.client.get(url, params=params, headers=headers)
            if r.status_code == 200:
                data = r.json()
                n = len(data) if isinstance(data, list) else "?"
                out[label] = f"200 OK — {n} items"
            else:
                out[label] = f"{r.status_code}: {r.text[:100]}"
        except Exception as e:
            out[label] = f"err: {str(e)[:80]}"
    return out


@app.get("/api/create-promo-template")
async def api_create_promo_template(waba_id: str = Query(None)):
    """Crea la plantilla de promos 'promo_club' via API de Meta."""
    from api import promos
    token = os.getenv("WHATSAPP_TOKEN", "")
    if not token:
        return {"ok": False, "error": "WHATSAPP_TOKEN no configurado"}
    if waba_id:
        os.environ["WABA_ID"] = waba_id  # override para esta sesion
    return await promos.create_promo_template(token)


@app.get("/api/create-alert-template")
async def api_create_alert_template():
    """Crea la plantilla UTILITY 'aviso_club' para alertas internas."""
    from api import promos
    token = os.getenv("WHATSAPP_TOKEN", "")
    return await promos.create_alert_template(token)


@app.get("/api/promo-template-status")
async def api_promo_template_status():
    """Estado de aprobacion de la plantilla de promos."""
    from api import promos
    token = os.getenv("WHATSAPP_TOKEN", "")
    return await promos.template_status(token)


@app.get("/api/test-notify")
async def api_test_notify():
    """Send a test WhatsApp message to the club owner's notify phone."""
    from whatsapp.sender import send_text
    phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
    token = os.getenv("WHATSAPP_TOKEN", "")
    notify = os.getenv("CLUB_NOTIFY_PHONE", "528342546466")
    if not phone_id or not token:
        return {"ok": False, "error": "WhatsApp credentials not configured"}
    try:
        await send_text(phone_id, token, notify,
            "🔔 *Prueba de notificaciones MatchBot*\n\n"
            "Si estás viendo este mensaje, las alertas al club funcionan correctamente. ✅\n\n"
            "Por este medio te llegarán: nuevas reservas, cancelaciones, "
            "premios de clientes frecuentes y alertas del sistema.")
        return {"ok": True, "sent_to": notify}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.get("/api/health-check")
async def api_health_check(send_alert: int = Query(0)):
    """Run the self-check now. ?send_alert=1 also sends the WhatsApp
    alert if there are problems (for testing the alert path)."""
    problems = await _run_health_check()
    if problems and send_alert:
        await _send_health_alert(problems)
    return {"ok": len(problems) == 0, "problems": problems}


# ─────────────────────────────────────────────────────
# CLUB ROUTING — map WhatsApp phone_number_id → club
# ─────────────────────────────────────────────────────

import os

# Env-based routing: PHONE_NUMBER_ID_PADEL → club_id 1, PHONE_NUMBER_ID_SALON → club_id 2
_PHONE_MAP = {}

def _build_phone_map():
    """Build phone_number_id → club mapping from env vars."""
    global _PHONE_MAP
    pairs = [
        (os.getenv("PHONE_NUMBER_ID_PADEL", ""), 1, "Club de Padel Victoria"),
        (os.getenv("PHONE_NUMBER_ID_SALON", ""), 2, "Salón Multiusos Victoria"),
    ]
    for phone_id, club_id, name in pairs:
        if phone_id:
            _PHONE_MAP[phone_id] = {
                "id": club_id,
                "name": name,
                "wa_phone_id": phone_id,
                "wa_token": os.getenv("WHATSAPP_TOKEN", ""),
            }

_build_phone_map()


def _resolve_club(phone_number_id: str) -> dict | None:
    """Resolve phone_number_id to club dict. Uses env vars ONLY.

    Only processes messages for explicitly configured phone numbers
    (PHONE_NUMBER_ID_PADEL, PHONE_NUMBER_ID_SALON). Messages from any
    other number (e.g. CotizaExpress / Aceromax) are ignored.
    """
    if phone_number_id in _PHONE_MAP:
        return _PHONE_MAP[phone_number_id]

    # No fallback - unknown numbers are intentionally ignored so that
    # MatchBot does not hijack messages meant for other bots/WABAs.
    return None


# ─────────────────────────────────────────────────────
# WHATSAPP WEBHOOK (Meta Cloud API)
# ─────────────────────────────────────────────────────

@app.get("/webhook")
async def webhook_verify(request: Request):
    """WhatsApp webhook verification (GET)."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.WA_VERIFY_TOKEN:
        logger.info("Webhook verified")
        return PlainTextResponse(challenge)

    raise HTTPException(403, "Verification failed")


@app.post("/webhook")
async def webhook_receive(request: Request):
    """WhatsApp incoming message webhook (POST)."""
    body = await request.json()

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])

            if not messages:
                continue

            # Get phone_number_id to identify which club
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id", "")

            # Map phone_number_id → club using env vars
            club = _resolve_club(phone_number_id)
            if not club:
                logger.warning(f"Unknown phone_number_id: {phone_number_id}")
                continue

            # Extract WhatsApp profile name from contacts
            contacts = value.get("contacts", [])
            wa_profile_name = ""
            if contacts:
                wa_profile_name = contacts[0].get("profile", {}).get("name", "")

            for msg in messages:
                wa_msg_id = msg.get("id", "")

                # Dedup
                now = datetime.now().timestamp()
                if wa_msg_id in _processed_msgs:
                    continue
                _processed_msgs[wa_msg_id] = now

                # Clean old dedup entries
                cutoff = now - MSG_DEDUP_WINDOW
                stale = [k for k, v in _processed_msgs.items() if v < cutoff]
                for k in stale:
                    del _processed_msgs[k]

                sender = msg.get("from", "")

                # Log inbound message
                execute("""
                    INSERT INTO wa_messages (club_id, wa_phone, direction, message_type, content, wa_message_id)
                    VALUES (%s, %s, 'inbound', %s, %s::jsonb, %s)
                """, [club["id"], sender, msg.get("type", ""), json.dumps(msg), wa_msg_id])

                # Interactive replies (button/list taps) are deliberate
                # single actions — process IMMEDIATELY, no buffering.
                if msg.get("type") == "interactive":
                    buf_key = f"{club['id']}:{sender}"
                    # Cancel any pending buffered text for this user
                    if buf_key in _buffer_tasks:
                        _buffer_tasks[buf_key].cancel()
                        _buffer_tasks.pop(buf_key, None)
                        _msg_buffers.pop(buf_key, None)
                    asyncio.create_task(
                        _process_single(dict(club), sender, msg, wa_profile_name)
                    )
                    continue

                # Typed text: buffer briefly to group rapid messages
                buf_key = f"{club['id']}:{sender}"
                _msg_buffers[buf_key].append(msg)

                if buf_key in _buffer_tasks:
                    _buffer_tasks[buf_key].cancel()

                _buffer_tasks[buf_key] = asyncio.create_task(
                    _process_buffered(buf_key, dict(club), sender, wa_profile_name)
                )

    return JSONResponse({"status": "ok"})


async def _process_single(club: dict, sender: str, msg: dict, profile_name: str = ""):
    """Process one message immediately (used for button/list replies)."""
    try:
        await handle_message(club, sender, msg, profile_name=profile_name)
    except Exception as e:
        logger.error(f"Error handling interactive from {sender}: {e}", exc_info=True)


async def _process_buffered(buf_key: str, club: dict, sender: str, profile_name: str = ""):
    """Wait for buffer window, then process the last message."""
    await asyncio.sleep(settings.MSG_BUFFER_SECONDS)

    messages = _msg_buffers.pop(buf_key, [])
    _buffer_tasks.pop(buf_key, None)

    if not messages:
        return

    # Process only the last message (user's final intent)
    last_msg = messages[-1]
    try:
        await handle_message(club, sender, last_msg, profile_name=profile_name)
    except Exception as e:
        logger.error(f"Error handling message from {sender}: {e}", exc_info=True)


# ─────────────────────────────────────────────────────
# ADMIN API — Courts
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/courts")
async def api_get_courts(club_id: int):
    """Get all courts for a club."""
    courts = execute(
        "SELECT * FROM courts WHERE club_id = %s AND active = TRUE ORDER BY sort_order",
        [club_id], fetch_all=True
    )
    return [dict(c) for c in courts]


# ─────────────────────────────────────────────────────
# ADMIN API — Availability
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/availability")
async def api_get_availability(
    club_id: int,
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    court_id: Optional[int] = None,
    court_type: Optional[str] = None,
):
    """Get available slots for a date."""
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD")

    slots = get_available_slots(club_id, target, court_id=court_id, court_type=court_type)
    return slots


# ─────────────────────────────────────────────────────
# ADMIN API — Bookings
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/bookings")
async def api_get_bookings(
    club_id: int,
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
):
    """Get all bookings for a date (admin calendar view)."""
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format")

    bookings = get_bookings_for_date(club_id, target)
    # Serialize time objects
    for b in bookings:
        b["start_time"] = str(b["start_time"])[:5]
        b["end_time"] = str(b["end_time"])[:5]
        b["booking_date"] = str(b["booking_date"])
        if b.get("created_at"):
            b["created_at"] = str(b["created_at"])
        if b.get("updated_at"):
            b["updated_at"] = str(b["updated_at"])
        if b.get("cancelled_at"):
            b["cancelled_at"] = str(b["cancelled_at"])
    return bookings


@app.post("/api/clubs/{club_id}/bookings")
async def api_create_booking(club_id: int, request: Request):
    """Create a booking from admin panel."""
    data = await request.json()
    try:
        booking = create_booking(
            club_id=club_id,
            court_id=data["court_id"],
            booking_date=date.fromisoformat(data["booking_date"]),
            start_time=data["start_time"],
            end_time=data["end_time"],
            wa_phone=data.get("wa_phone", "admin"),
            customer_name=data.get("customer_name"),
            booking_type=data.get("booking_type", "regular"),
            payment_method=data.get("payment_method"),
            amount_cents=data.get("amount_cents", 0),
            notes=data.get("notes"),
        )
        # Serialize
        booking["start_time"] = str(booking["start_time"])[:5]
        booking["end_time"] = str(booking["end_time"])[:5]
        booking["booking_date"] = str(booking["booking_date"])
        return booking
    except BookingError as e:
        raise HTTPException(409, str(e))


@app.patch("/api/clubs/{club_id}/bookings/{booking_id}/confirm")
async def api_confirm_booking(club_id: int, booking_id: int, request: Request):
    """Confirm/pay a booking from admin."""
    data = await request.json()
    try:
        booking = confirm_booking(booking_id, data.get("payment_method", "cash"))
        return {"status": "confirmed", "booking_id": booking_id}
    except BookingError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/clubs/{club_id}/bookings/{booking_id}/cancel")
async def api_cancel_booking(club_id: int, booking_id: int):
    """Cancel a booking from admin."""
    try:
        cancel_booking(booking_id, club_id)
        return {"status": "cancelled", "booking_id": booking_id}
    except BookingError as e:
        raise HTTPException(400, str(e))


# ─────────────────────────────────────────────────────
# ADMIN API — Customers
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/customers")
async def api_get_customers(
    club_id: int,
    search: Optional[str] = None,
    limit: int = 50,
):
    """List customers for a club."""
    if search:
        customers = execute("""
            SELECT * FROM customers
            WHERE club_id = %s AND (name ILIKE %s OR phone ILIKE %s)
            ORDER BY last_booking DESC NULLS LAST
            LIMIT %s
        """, [club_id, f"%{search}%", f"%{search}%", limit], fetch_all=True)
    else:
        customers = execute("""
            SELECT * FROM customers WHERE club_id = %s
            ORDER BY last_booking DESC NULLS LAST LIMIT %s
        """, [club_id, limit], fetch_all=True)

    return [dict(c) for c in customers]


# ─────────────────────────────────────────────────────
# ADMIN API — Dashboard Stats
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/stats")
async def api_get_stats(club_id: int):
    """Dashboard summary stats."""
    today = date.today()

    today_bookings = execute(
        "SELECT COUNT(*) as cnt FROM bookings WHERE club_id = %s AND booking_date = %s AND status != 'cancelled'",
        [club_id, today], fetch_one=True
    )
    today_revenue = execute(
        "SELECT COALESCE(SUM(amount_cents), 0) as total FROM bookings WHERE club_id = %s AND booking_date = %s AND payment_status = 'paid'",
        [club_id, today], fetch_one=True
    )
    total_customers = execute(
        "SELECT COUNT(*) as cnt FROM customers WHERE club_id = %s",
        [club_id], fetch_one=True
    )

    return {
        "today_bookings": today_bookings["cnt"],
        "today_revenue_cents": today_revenue["total"],
        "total_customers": total_customers["cnt"],
    }


# ─────────────────────────────────────────────────────
# ADMIN API — Playtomic (list/cancel matches)
# ─────────────────────────────────────────────────────

@app.get("/api/playtomic/matches")
async def api_playtomic_matches(
    date: str = Query(None, description="Date YYYY-MM-DD (optional, shows all if omitted)"),
):
    """List Playtomic matches. Optionally filter by local date."""
    if date:
        matches = await playtomic.list_matches(date)
    else:
        matches = await playtomic.list_all_matches_raw()

    # Load resource names for display
    await playtomic._ensure_resource_names()

    # Summarize for easy reading
    summary = []
    for m in matches:
        resource_id = m.get("resource_id", "")
        court_name = playtomic._resource_names.get(resource_id, resource_id[:8])
        start_utc = m.get("start_date", m.get("start", ""))
        end_utc = m.get("end_date", m.get("end", ""))

        # Convert to local time for display
        local_start = ""
        local_end = ""
        if start_utc and "T" in start_utc:
            local_start = playtomic._local_time_str(start_utc.split("T")[1][:5])
        if end_utc and "T" in end_utc:
            local_end = playtomic._local_time_str(end_utc.split("T")[1][:5])

        # Extract player info
        players = []
        for team in m.get("teams", []):
            for p in team.get("players", []):
                name = p.get("name", "")
                phone = p.get("phone", "")
                if name or phone:
                    players.append({"name": name, "phone": phone})

        summary.append({
            "match_id": m.get("match_id", ""),
            "court": court_name,
            "resource_id": resource_id,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "local_time": f"{local_start}-{local_end}" if local_start else "",
            "status": m.get("status", ""),
            "players": players,
        })
    return {"count": len(matches), "matches": summary}


@app.delete("/api/playtomic/matches/{match_id}")
async def api_playtomic_cancel(match_id: str):
    """Cancel a Playtomic match by ID (admin)."""
    result = await playtomic.cancel_match(match_id)
    if result.get("success"):
        return {"status": "cancelled", "match_id": match_id}
    raise HTTPException(400, result.get("error", "Cancel failed"))


@app.get("/api/playtomic/cancel-match")
async def api_playtomic_cancel_get(match_id: str = Query(...)):
    """TEMP: Cancel a match via GET (for remote admin without curl DELETE)."""
    result = await playtomic.cancel_match(match_id)
    return result


@app.get("/api/playtomic/matches-list")
async def api_playtomic_matches_list(date: str = Query(...)):
    """TEMP: List matches for a local date — id, name, court, time."""
    matches = await playtomic.list_matches(date)
    out = []
    for m in matches:
        players = []
        for t in m.get("teams", []):
            for p in t.get("players", []):
                players.append(p.get("name", "?"))
        out.append({
            "match_id": m.get("match_id"),
            "start": m.get("start_date"),
            "resource": m.get("resource_name"),
            "players": players,
            "owner_id": m.get("owner_id"),
            "origin": m.get("match_origin"),
        })
    return {"count": len(out), "matches": out,
            "list_debug": getattr(playtomic, "_last_list_debug", {})}


@app.get("/api/playtomic/match-raw")
async def api_playtomic_match_raw(match_id: str = Query(...)):
    """TEMP: Fetch raw match JSON to diff working vs broken bookings."""
    await playtomic.ensure_tenant_auth()
    if not playtomic.tenant_token:
        return {"error": "no tenant token"}
    headers = {
        "Authorization": f"Bearer {playtomic.tenant_token}",
        "x-requested-with": "com.playtomic.manager 1.299.0+build.5655",
        "x-authorization-scope": f"tenant:{os.getenv('PLAYTOMIC_TENANT_ID', '')}",
    }
    try:
        r = await playtomic.client.get(
            f"https://manager.playtomic.io/api/v1/matches/{match_id}",
            headers=headers,
        )
        if r.status_code == 200:
            return r.json()
        return {"error": f"{r.status_code}: {r.text[:300]}"}
    except Exception as e:
        return {"error": str(e)[:300]}


@app.post("/api/playtomic/cancel-bulk")
async def api_playtomic_cancel_bulk(request: Request):
    """Cancel multiple Playtomic matches at once (admin)."""
    data = await request.json()
    match_ids = data.get("match_ids", [])
    results = []
    for mid in match_ids:
        r = await playtomic.cancel_match(mid)
        results.append({"match_id": mid, **r})
    return {"results": results}


# ─────────────────────────────────────────────────────
# ADMIN API — Playtomic Diagnostics
# ─────────────────────────────────────────────────────

@app.get("/api/playtomic/search")
async def api_playtomic_search(date: str = Query("2026-07-05")):
    """Search for bookings across multiple Playtomic API endpoints."""
    return await playtomic.search_bookings(date)


@app.get("/api/playtomic/debug")
async def api_playtomic_debug(date: str = Query(None)):
    """Debug: show raw Playtomic responses for tenant info + availability."""
    import httpx
    from datetime import date as d, timedelta

    if not date:
        offset = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
        local_now = datetime.utcnow() + timedelta(hours=offset)
        date = local_now.strftime("%Y-%m-%d")

    tenant_id = os.getenv("PLAYTOMIC_TENANT_ID", "")
    api = "https://manager.playtomic.io/api"
    results = {"code_version": "v27-ranged-list", "date": date, "tenant_id": tenant_id}

    # Show bot auth status
    results["bot_logged_in"] = playtomic.token is not None
    results["bot_user_id"] = playtomic.user_id
    results["bot_resource_names"] = playtomic._resource_names
    results["bot_resource_sports"] = playtomic._resource_sports

    # Try login now if not logged in
    if not playtomic.token:
        try:
            login_url = f"{api}/v3/auth/login"
            login_r = await playtomic.client.post(login_url, json={
                "email": os.getenv("PLAYTOMIC_EMAIL", ""),
                "password": "***",  # masked
                # New Aug-2026 contract: scopes required at login
                "requested_user_scopes": [
                    {"role": "ROLE_ACTIVITY_MANAGER", "scope_id": tenant_id},
                    {"role": "ROLE_TENANT_MANAGER", "scope_id": tenant_id},
                ],
            })
            results["login_test"] = {
                "url": login_url,
                "status": login_r.status_code,
                "response": login_r.text[:300],
                "email_set": bool(os.getenv("PLAYTOMIC_EMAIL")),
                "password_set": bool(os.getenv("PLAYTOMIC_PASSWORD")),
            }
            # Also try the real login
            login_ok = await playtomic.login()
            results["login_retry"] = login_ok
            results["bot_logged_in_after_retry"] = playtomic.token is not None
        except Exception as e:
            results["login_test_error"] = str(e)

    # Test availability via bot's own method
    try:
        bot_avail = await playtomic.get_availability(date)
        results["bot_availability_courts"] = len(bot_avail)
        results["bot_availability_slots"] = sum(len(c.get("slots", [])) for c in bot_avail)
        if bot_avail:
            results["bot_availability_preview"] = [
                {"name": c["name"], "slots": len(c["slots"])} for c in bot_avail
            ]
    except Exception as e:
        results["bot_availability_error"] = str(e)

    # Per-request status codes from the bot's availability queries
    results["avail_query_debug"] = getattr(playtomic, "_last_avail_debug", {})
    results["has_tenant_token"] = playtomic.tenant_token is not None
    # Last booking attempt result (statuses + error bodies)
    results["last_booking_debug"] = getattr(playtomic, "_last_booking_debug", {})

    # Check the OTHER tenant ("TO BE DELETED") — the bot's manager role
    # may be attached to it instead of the tenant we use
    ALT_TENANT = "04007e09-6f59-4ed1-bbef-13c80aa0c906"
    try:
        r = await playtomic.client.get(
            f"{api}/v1/tenants/{ALT_TENANT}",
            headers=playtomic._auth_headers(),
        )
        if r.status_code == 200:
            info = r.json()
            alt_resources = info.get("resources", info.get("facilities", []))
            results["alt_tenant_name"] = info.get("tenant_name", info.get("name", "?"))
            results["alt_tenant_resources"] = [
                {"id": x.get("resource_id", x.get("id", "")), "name": x.get("name", ""), "sport": x.get("sport_id", "")}
                for x in (alt_resources[:10] if isinstance(alt_resources, list) else [])
            ]
        else:
            results["alt_tenant_error"] = f"{r.status_code}: {r.text[:150]}"
    except Exception as e:
        results["alt_tenant_error"] = str(e)[:200]
    # Claims of the current tenant token (roles = manager permissions)
    results["tenant_token_claims"] = getattr(playtomic, "_tenant_token_claims", {})
    # Claims of the customer (login) token — do the roles exist at login?
    if playtomic.token:
        c = playtomic._decode_token_claims(playtomic.token)
        results["customer_token_claims"] = {
            "aud": c.get("aud"),
            "scopes": c.get("scopes"),
            "roles": [k for k in c.keys() if k.startswith("role")],
        }

    async with httpx.AsyncClient(timeout=10, http2=True, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://playtomic.io",
        "Referer": "https://playtomic.io/",
    }) as client:
        # 1. Tenant info
        try:
            r = await client.get(f"{api}/v1/tenants/{tenant_id}")
            info = r.json() if r.status_code == 200 else r.text
            # Extract just the keys and resources summary
            if isinstance(info, dict):
                resources = info.get("resources", info.get("facilities", []))
                results["tenant_keys"] = list(info.keys())
                results["resources_count"] = len(resources)
                results["resources"] = [
                    {k: v for k, v in r.items() if k in ("resource_id", "id", "name", "resource_name", "sport_id")}
                    for r in (resources[:10] if isinstance(resources, list) else [])
                ]
            else:
                results["tenant_raw"] = str(info)[:500]
        except Exception as e:
            results["tenant_error"] = str(e)

        # 2. Availability raw (first 2 items)
        try:
            offset_h = abs(int(os.getenv("CLUB_UTC_OFFSET", "-6")))
            from datetime import date as d, timedelta
            target = d.fromisoformat(date)
            next_day = (target + timedelta(days=1)).isoformat()

            r = await client.get(f"{api}/v1/availability", params={
                "user_id": "me", "sport_id": "PADEL", "tenant_id": tenant_id,
                "start_min": f"{date}T{offset_h:02d}:00:00",
                "start_max": f"{next_day}T{offset_h:02d}:00:00",
            })
            if r.status_code == 200:
                avail = r.json()
                results["availability_count"] = len(avail) if isinstance(avail, list) else "not a list"
                if isinstance(avail, list) and avail:
                    results["availability_sample_keys"] = list(avail[0].keys())
                    # Show first item with limited slots
                    sample = dict(avail[0])
                    if "slots" in sample:
                        sample["slots"] = sample["slots"][:3]
                    results["availability_sample"] = sample
            else:
                results["availability_error"] = f"{r.status_code}: {r.text[:300]}"
        except Exception as e:
            results["availability_error"] = str(e)

    # 3. Cached resource names
    results["cached_resource_names"] = playtomic._resource_names

    return results


# ─────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────

@app.get("/")
async def root_redirect():
    """matchbot.live → panel de administración."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/panel", status_code=302)


@app.get("/panel", response_class=HTMLResponse)
async def admin_panel(request: Request, date: str = Query(None)):
    """Panel de administración estilo Playtomic Manager (requiere login)."""
    from fastapi.responses import RedirectResponse
    from api import auth
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/panel/login", status_code=302)
    from api.panel import render_panel
    return HTMLResponse(await render_panel(playtomic, date, user=user))


@app.get("/panel/login", response_class=HTMLResponse)
async def panel_login_page():
    from api import auth
    return HTMLResponse(auth.login_page())


@app.post("/panel/login")
async def panel_login(request: Request):
    from fastapi.responses import RedirectResponse
    from api import auth
    form = await request.form()
    user = auth.login(form.get("username", ""), form.get("password", ""))
    if not user:
        return HTMLResponse(auth.login_page("Usuario o contraseña incorrectos."),
                            status_code=401)
    resp = RedirectResponse("/panel", status_code=302)
    resp.set_cookie(auth.COOKIE, auth.make_token(user), httponly=True,
                    max_age=auth.SESSION_HOURS * 3600, samesite="lax")
    return resp


@app.get("/panel/logout")
async def panel_logout():
    from fastapi.responses import RedirectResponse
    from api import auth
    resp = RedirectResponse("/panel/login", status_code=302)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.post("/api/panel/book")
async def panel_create_booking(request: Request, ajax: int = Query(0)):
    """Crear reserva desde el calendario del panel (staff o superadmin)."""
    from api import auth, customers, loyalty
    user = auth.current_user(request)
    if not user:
        if ajax:
            return {"ok": False, "error": "Sesión expirada, vuelve a entrar"}
        return HTMLResponse("<h3>❌ Inicia sesión para crear reservas.</h3>"
                            "<a href='/panel/login'>→ Iniciar sesión</a>", status_code=403)
    form = await request.form()
    court = (form.get("court") or "").strip()
    date_str = (form.get("date") or "").strip()
    time_str = (form.get("time") or "").strip()[:5]
    name = (form.get("name") or "").strip()
    phone = "".join(ch for ch in (form.get("phone") or "") if ch.isdigit())
    cobro = form.get("cobro") or "single"
    try:
        duration = int(form.get("duration") or 90)
        players = int(form.get("players") or 4)
    except ValueError:
        duration, players = 90, 4
    back = f"<p><a href='/panel?date={date_str}'>← Volver al calendario</a></p>"
    if not (court and date_str and time_str and name):
        if ajax:
            return {"ok": False, "error": "Faltan datos (cancha, fecha, hora o nombre)"}
        return HTMLResponse(f"<h3>❌ Faltan datos (cancha, fecha, hora o nombre).</h3>{back}",
                            status_code=400)

    # Nombre de cancha → resource_id
    try:
        await playtomic._ensure_resource_names()
        resource_id = next((rid for rid, nm in playtomic._resource_names.items()
                            if nm == court), None)
    except Exception as e:
        logger.error(f"panel book resource lookup: {e}")
        resource_id = None
    if not resource_id:
        if ajax:
            return {"ok": False, "error": f"No encontré la cancha '{court}'"}
        return HTMLResponse(f"<h3>❌ No encontré la cancha '{court}'.</h3>{back}",
                            status_code=400)

    # Hora local → UTC
    offset = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
    try:
        from datetime import datetime as _dt, timedelta as _td
        local_dt = _dt.fromisoformat(f"{date_str}T{time_str}:00")
        utc_dt = local_dt - _td(hours=offset)
        start_iso = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        if ajax:
            return {"ok": False, "error": "Fecha u hora inválida"}
        return HTMLResponse(f"<h3>❌ Fecha u hora inválida.</h3>{back}", status_code=400)

    price = 0.0
    try:
        price = await playtomic.calculate_price(resource_id, start_iso, duration) or 0.0
    except Exception as e:
        logger.error(f"panel book pricing: {e}")
    if price <= 0:
        price = float(os.getenv("FALLBACK_PRICE", "300"))

    cobro_txt = ("PAGO ÚNICO" if cobro == "single"
                 else f"DIVIDIDO entre {players} (${price/max(players,1):.0f} c/u)")
    notes = f"🖥️ Creada desde el panel por {user.get('name') or user.get('sub')} — {cobro_txt}"

    result = await playtomic.create_booking(
        resource_id=resource_id, start_time=start_iso, duration=duration,
        customer_name=name, customer_phone=phone, slot_price=price,
        private_notes=notes, players=players, payment_mode=cobro)

    if not result.get("success"):
        err = "ese horario se acaba de ocupar" if result.get("error") == "slot_locked" \
              else f"error técnico ({str(result.get('error'))[:120]})"
        if ajax:
            return {"ok": False, "error": f"No se pudo crear: {err}"}
        return HTMLResponse(f"<h3>😕 No se pudo crear la reserva: {err}.</h3>{back}",
                            status_code=502)

    # Cliente + sello si dio teléfono
    if phone and len(phone) >= 10:
        try:
            full = phone if len(phone) > 10 else "52" + phone
            customers.touch_booking(full, source="panel")
            if not customers.registered_name(full):
                customers.save_name(full, name.title(), source="panel")
            loyalty.record_booking(full, result.get("match_id", ""), start_iso)
        except Exception as e:
            logger.error(f"panel book customer/loyalty: {e}")

    detalle = (f"{court} · {date_str} {time_str} · {duration} min · "
               f"${price:.0f} MXN · {cobro_txt.lower()}")
    if ajax:
        return {"ok": True, "name": name, "detalle": detalle}
    return HTMLResponse(f"<h3>✅ Reserva creada para <b>{name}</b></h3>"
                        f"<p>{detalle}</p>{back}")


def _panel_user_or_none(request):
    from api import auth
    return auth.current_user(request)


@app.post("/api/panel/torneos")
async def panel_create_tournament(request: Request):
    """Crear torneo/liga desde el panel."""
    from fastapi.responses import RedirectResponse
    from api import tournaments
    if not _panel_user_or_none(request):
        return HTMLResponse("<h3>❌ Inicia sesión.</h3><a href='/panel/login'>→ Entrar</a>",
                            status_code=403)
    form = await request.form()
    players = [ln for ln in (form.get("players") or "").splitlines() if ln.strip()]
    if len(players) < 3 or not (form.get("name") or "").strip():
        return HTMLResponse("<h3>❌ Pon un nombre y al menos 3 participantes.</h3>"
                            "<a href='/panel'>← Volver</a>", status_code=400)
    tid = tournaments.create((form.get("name") or "").strip(),
                             form.get("sport") or "padel",
                             form.get("fmt") or "grupos", players)
    return RedirectResponse(f"/panel/torneo/{tid}", status_code=302)


@app.get("/panel/torneo/{tid}", response_class=HTMLResponse)
async def panel_tournament_detail(request: Request, tid: int):
    from fastapi.responses import RedirectResponse
    from api import tournaments
    if not _panel_user_or_none(request):
        return RedirectResponse("/panel/login", status_code=302)
    t = tournaments.get(tid)
    if not t:
        return HTMLResponse("<h3>Torneo no encontrado.</h3><a href='/panel'>← Volver</a>",
                            status_code=404)
    return HTMLResponse(tournaments.render_detail(t))


@app.post("/api/panel/torneos/{tid}/score")
async def panel_tournament_score(request: Request, tid: int):
    from fastapi.responses import RedirectResponse
    from api import tournaments
    if not _panel_user_or_none(request):
        return RedirectResponse("/panel/login", status_code=302)
    form = await request.form()
    try:
        tournaments.set_score(tid, int(form.get("g")), int(form.get("f")),
                              int(form.get("sa")), int(form.get("sb")))
    except (TypeError, ValueError):
        pass
    return RedirectResponse(f"/panel/torneo/{tid}", status_code=302)


@app.post("/api/panel/torneos/{tid}/bracket")
async def panel_tournament_bracket(request: Request, tid: int):
    from fastapi.responses import RedirectResponse
    from api import tournaments
    if not _panel_user_or_none(request):
        return RedirectResponse("/panel/login", status_code=302)
    tournaments.gen_bracket(tid)
    return RedirectResponse(f"/panel/torneo/{tid}", status_code=302)


@app.post("/api/panel/torneos/{tid}/winner")
async def panel_tournament_winner(request: Request, tid: int):
    from fastapi.responses import RedirectResponse
    from api import tournaments
    if not _panel_user_or_none(request):
        return RedirectResponse("/panel/login", status_code=302)
    form = await request.form()
    try:
        tournaments.set_winner(tid, int(form.get("r")), int(form.get("m")),
                               int(form.get("w")))
    except (TypeError, ValueError):
        pass
    return RedirectResponse(f"/panel/torneo/{tid}", status_code=302)


@app.post("/api/panel/cancel")
async def panel_cancel_booking(request: Request):
    """Cancelar una reserva desde el panel (clic en el cuadro → Cancelar)."""
    from api import auth, loyalty
    user = auth.current_user(request)
    if not user:
        return {"ok": False, "error": "Sesión expirada, vuelve a entrar"}
    form = await request.form()
    match_id = (form.get("match_id") or "").strip()
    if not match_id:
        return {"ok": False, "error": "Falta la reserva"}
    result = await playtomic.cancel_match(match_id)
    if result.get("error") or not result.get("success"):
        return {"ok": False,
                "error": f"No se pudo cancelar ({str(result.get('error') or result)[:120]})"}
    try:
        loyalty.mark_cancelled(match_id)
    except Exception as e:
        logger.error(f"panel cancel loyalty: {e}")
    logger.info(f"Panel: reserva {match_id} cancelada por {user.get('sub')}")
    return {"ok": True}


@app.post("/api/panel/move")
async def panel_move_booking(request: Request):
    """Mover una reserva (drag & drop): crea en el nuevo lugar y cancela la vieja."""
    from api import auth
    user = auth.current_user(request)
    if not user:
        return {"ok": False, "error": "Sesión expirada, vuelve a entrar"}
    form = await request.form()
    match_id = (form.get("match_id") or "").strip()
    court = (form.get("court") or "").strip()
    date_str = (form.get("date") or "").strip()
    time_str = (form.get("time") or "").strip()[:5]
    try:
        duration = int(form.get("duration") or 90)
    except ValueError:
        duration = 90
    if not (match_id and court and date_str and time_str):
        return {"ok": False, "error": "Faltan datos del movimiento"}

    # Datos de la reserva original
    old = await playtomic.get_match(match_id)
    if not old:
        return {"ok": False, "error": "No encontré la reserva original"}
    if "CANCEL" in str(old.get("status", "")).upper():
        return {"ok": False, "error": "Esa reserva ya está cancelada"}
    name, phone = "", ""
    for t in old.get("teams", []):
        for p in t.get("players", []):
            if p.get("name"):
                name = p["name"]
                break
        if name:
            break
    # El bot guarda "Nombre (teléfono)" — separar
    if "(" in name and name.rstrip().endswith(")"):
        base, tel = name.rsplit("(", 1)
        name, phone = base.strip(), "".join(c for c in tel if c.isdigit())
    old_pay = str((old.get("registration_info") or {}).get("payment_type", "")).upper()
    payment_mode = "single" if old_pay == "SINGLE_PAYER" else "split"
    notes = old.get("private_notes") or ""
    move_tag = f"↪️ Movida desde el panel por {user.get('name') or user.get('sub')}"
    notes = f"{notes} | {move_tag}" if notes else move_tag

    try:
        await playtomic._ensure_resource_names()
        resource_id = next((rid for rid, nm in playtomic._resource_names.items()
                            if nm == court), None)
    except Exception:
        resource_id = None
    if not resource_id:
        return {"ok": False, "error": f"No encontré la cancha '{court}'"}

    offset = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
    try:
        from datetime import datetime as _dt, timedelta as _td
        local_dt = _dt.fromisoformat(f"{date_str}T{time_str}:00")
        start_iso = (local_dt - _td(hours=offset)).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return {"ok": False, "error": "Fecha u hora inválida"}

    price = 0.0
    try:
        price = await playtomic.calculate_price(resource_id, start_iso, duration) or 0.0
    except Exception:
        pass
    if price <= 0:
        price = float(os.getenv("FALLBACK_PRICE", "300"))

    # 1) Crear primero en el nuevo lugar (si falla, la original queda intacta)
    result = await playtomic.create_booking(
        resource_id=resource_id, start_time=start_iso, duration=duration,
        customer_name=name or "Reserva", customer_phone=phone,
        slot_price=price, private_notes=notes, payment_mode=payment_mode,
        source="panel")
    if not result.get("success"):
        err = "ese horario ya está ocupado" if result.get("error") == "slot_locked" \
              else f"error al crear ({str(result.get('error'))[:100]})"
        return {"ok": False, "error": f"No se movió: {err}. La reserva original sigue igual"}

    # 2) Cancelar la original
    try:
        cancel = await playtomic.cancel_match(match_id)
        if cancel.get("error"):
            logger.error(f"move: cancel original failed: {cancel}")
            return {"ok": True,
                    "warning": "Se creó la nueva pero NO pude cancelar la original — cancélala a mano"}
        from api import loyalty as _loy
        _loy.mark_cancelled(match_id)
        if phone:
            full = phone if len(phone) > 10 else "52" + phone
            _loy.record_booking(full, result.get("match_id", ""), start_iso)
    except Exception as e:
        logger.error(f"move: cancel step failed: {e}")
    return {"ok": True}


@app.post("/api/panel/settings")
async def panel_save_settings(request: Request):
    """Guardar la configuración del club (solo superadmin)."""
    from api import auth, settings as st
    user = auth.current_user(request)
    if not user or user.get("role") != "superadmin":
        return HTMLResponse("<h3>❌ Solo el super admin puede cambiar la configuración.</h3>"
                            "<a href='/panel'>← Volver</a>", status_code=403)
    form = await request.form()
    saved = []
    for key in st.DEFAULTS:
        if key in form:
            st.set_value(key, str(form.get(key) or ""))
            saved.append(key)
    return HTMLResponse(f"<h3>✅ Configuración guardada ({len(saved)} valores).</h3>"
                        f"<p>Los cambios aplican en ~1 minuto.</p>"
                        f"<a href='/panel'>← Volver al panel</a>")


@app.get("/api/mirror/status")
async def mirror_status():
    """Estado del espejo local de reservas."""
    row = execute("SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM bookings_mirror",
                  fetch_one=True) or {}
    by_src = execute("SELECT source, COUNT(*) AS n FROM bookings_mirror GROUP BY source",
                     fetch_all=True) or []
    return {"total": int(row.get("n") or 0),
            "ultima_actualizacion": str(row.get("last") or ""),
            "por_fuente": {r["source"]: int(r["n"]) for r in by_src}}


@app.post("/api/mirror/sync")
async def mirror_sync_now(request: Request):
    """Forzar sincronización del espejo (X-Voice-Key)."""
    from api import voice, mirror
    if not voice._auth_ok(request):
        raise HTTPException(403, "X-Voice-Key invalida")
    return await mirror.sync(playtomic)


@app.post("/api/panel/reset-admin")
async def panel_reset_admin(request: Request):
    """Rescate: recrea el superadmin 'alejandro'. Requiere X-Voice-Key.
    Password = env SUPERADMIN_PASSWORD (o VOICE_API_KEY si no está)."""
    from api import auth, voice
    if not voice._auth_ok(request):
        raise HTTPException(403, "X-Voice-Key invalida")
    pwd = os.getenv("SUPERADMIN_PASSWORD", "") or os.getenv("VOICE_API_KEY", "")
    if not pwd:
        return {"ok": False, "error": "Sin SUPERADMIN_PASSWORD ni VOICE_API_KEY"}
    try:
        auth.ensure_table()
        auth.create_user("alejandro", pwd, "Alejandro", "superadmin")
        row = execute("SELECT COUNT(*) AS n FROM panel_users", fetch_one=True)
        return {"ok": True, "users": int(row["n"]) if row else 0,
                "msg": "Superadmin 'alejandro' recreado con la password de SUPERADMIN_PASSWORD (o la voice key)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/panel/users")
async def panel_create_user(request: Request):
    """Crear/actualizar usuario del panel (solo superadmin). ?off=usuario desactiva."""
    from api import auth
    user = auth.current_user(request)
    if not user or user.get("role") != "superadmin":
        return HTMLResponse("<h3>❌ Solo el super admin puede administrar usuarios.</h3>"
                            "<a href='/panel'>← Volver</a>", status_code=403)
    off = request.query_params.get("off", "")
    if off:
        auth.set_active(off, False)
        return HTMLResponse(f"<h3>✅ Usuario '{off}' desactivado.</h3>"
                            f"<a href='/panel'>← Volver al panel</a>")
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    name = (form.get("name") or "").strip()
    role = form.get("role") or "staff"
    if not username or len(password) < 6:
        return HTMLResponse("<h3>❌ Usuario requerido y contraseña de mínimo 6 caracteres.</h3>"
                            "<a href='/panel'>← Volver</a>", status_code=400)
    auth.create_user(username, password, name, role)
    return HTMLResponse(f"<h3>✅ Usuario '{username}' guardado ({role}).</h3>"
                        f"<a href='/panel'>← Volver al panel</a>")


@app.get("/reservas", response_class=HTMLResponse)
async def reservas_dashboard():
    """Dashboard de reservas: hoy y próximos días, desde Playtomic."""
    from datetime import timedelta as _td
    offset = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
    now_local = datetime.utcnow() + _td(hours=offset)
    today_local = now_local.date()

    matches = await playtomic.list_matches()

    DAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    MONTHS = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

    # Parse + filter: from today (local) onwards, not cancelled
    upcoming = []
    for m in matches:
        try:
            start_utc = datetime.fromisoformat(str(m.get("start_date", ""))[:19])
        except ValueError:
            continue
        start_local = start_utc + _td(hours=offset)
        if start_local.date() < today_local:
            continue
        status = str(m.get("status", "")).upper()
        if "CANCEL" in status:
            continue
        players = []
        is_bot = False
        for t in m.get("teams", []):
            for p in t.get("players", []):
                nm = p.get("name") or ""
                if nm:
                    players.append(nm)
                mid = str(p.get("merchant_player_id") or "")
                # Bot bookings: guest id + phone digits in the name
                if mid.startswith("guest:") and "(" in nm and ")" in nm:
                    is_bot = True
        upcoming.append({
            "local": start_local,
            "court": m.get("resource_name", "?"),
            "players": ", ".join(players) or "—",
            "status": status,
            "bot": is_bot,
            "sport": m.get("sport_id", ""),
        })

    upcoming.sort(key=lambda x: x["local"])

    n_today = sum(1 for u in upcoming if u["local"].date() == today_local)
    n_tomorrow = sum(1 for u in upcoming if u["local"].date() == today_local + _td(days=1))
    n_week = sum(1 for u in upcoming if u["local"].date() <= today_local + _td(days=7))
    n_bot = sum(1 for u in upcoming if u["bot"])
    pct_bot = round(100 * n_bot / len(upcoming)) if upcoming else 0

    # Group by day
    groups = {}
    for u in upcoming:
        groups.setdefault(u["local"].date(), []).append(u)

    rows_html = ""
    for day in sorted(groups.keys()):
        if day == today_local:
            day_label = f"HOY · {DAYS[day.weekday()]} {day.day} {MONTHS[day.month]}"
        elif day == today_local + _td(days=1):
            day_label = f"MAÑANA · {DAYS[day.weekday()]} {day.day} {MONTHS[day.month]}"
        else:
            day_label = f"{DAYS[day.weekday()]} {day.day} {MONTHS[day.month]}"
        rows_html += f'<tr><td colspan="4" class="day-header">{day_label} <span class="day-count">({len(groups[day])} reservas)</span></td></tr>'
        for u in groups[day]:
            hora = u["local"].strftime("%I:%M %p").lstrip("0")
            badge = '<span class="badge bot">🤖 Bot</span>' if u["bot"] else '<span class="badge club">🏢 Club</span>'
            sport_icon = "⚽" if "FOOT" in u["sport"] else "🎾"
            rows_html += (
                f'<tr><td class="hora">{hora}</td>'
                f'<td>{sport_icon} {u["court"]}</td>'
                f'<td class="players">{u["players"]}</td>'
                f'<td>{badge}</td></tr>'
            )

    if not rows_html:
        rows_html = '<tr><td colspan="4" style="text-align:center;padding:2rem;color:#6b7280;">No hay reservas próximas</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reservas — MatchBot</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#0f172a; color:#e2e8f0; padding:1.5rem; }}
h1 {{ font-size:1.3rem; margin-bottom:0.25rem; }}
.sub {{ color:#64748b; font-size:0.8rem; margin-bottom:1.5rem; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:0.75rem; margin-bottom:1.5rem; }}
.card {{ background:#1e293b; border:1px solid #334155; border-radius:12px; padding:1rem; }}
.card .num {{ font-size:1.8rem; font-weight:800; color:#10b981; }}
.card .lbl {{ font-size:0.7rem; color:#94a3b8; text-transform:uppercase; letter-spacing:0.05em; margin-top:0.25rem; }}
table {{ width:100%; border-collapse:collapse; background:#1e293b; border-radius:12px; overflow:hidden; }}
td {{ padding:0.6rem 0.9rem; font-size:0.85rem; border-bottom:1px solid #27354a; }}
.day-header {{ background:#0c4a37; color:#6ee7b7; font-weight:800; font-size:0.78rem; letter-spacing:0.04em; }}
.day-count {{ color:#94a3b8; font-weight:400; }}
.hora {{ font-weight:700; color:#f59e0b; white-space:nowrap; }}
.players {{ color:#cbd5e1; }}
.badge {{ font-size:0.68rem; font-weight:700; padding:2px 8px; border-radius:6px; }}
.badge.bot {{ background:#064e3b; color:#6ee7b7; }}
.badge.club {{ background:#1e3a8a; color:#93c5fd; }}
.links {{ margin-top:1.25rem; font-size:0.8rem; }}
.links a {{ color:#10b981; text-decoration:none; margin-right:1rem; }}
</style></head><body>
<h1>📅 Reservas — Club de Padel Victoria</h1>
<div class="sub">Actualizado: {now_local.strftime("%d/%m/%Y %I:%M %p").lstrip("0")} (hora local) · Recarga la página para actualizar</div>
<div class="cards">
  <div class="card"><div class="num">{n_today}</div><div class="lbl">Hoy</div></div>
  <div class="card"><div class="num">{n_tomorrow}</div><div class="lbl">Mañana</div></div>
  <div class="card"><div class="num">{n_week}</div><div class="lbl">Próximos 7 días</div></div>
  <div class="card"><div class="num">{pct_bot}%</div><div class="lbl">Hechas por el bot</div></div>
</div>
<table>{rows_html}</table>
<div class="links"><a href="/stats">📊 Uso del bot</a><a href="/reservas">🔄 Actualizar</a></div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/stats", response_class=HTMLResponse)
async def bot_stats():
    """Bot usage dashboard — visual stats page."""
    # Total unique users
    total_users = execute(
        "SELECT COUNT(DISTINCT wa_phone) AS cnt FROM wa_booking_state",
        fetch_one=True,
    )
    # Active last 7 days
    active_7d = execute(
        "SELECT COUNT(DISTINCT wa_phone) AS cnt FROM wa_booking_state WHERE updated_at >= NOW() - INTERVAL '7 days'",
        fetch_one=True,
    )
    # Active today
    active_today = execute(
        "SELECT COUNT(DISTINCT wa_phone) AS cnt FROM wa_booking_state WHERE updated_at::date = CURRENT_DATE",
        fetch_one=True,
    )
    # Daily activity last 14 days
    daily_activity = execute(
        """SELECT updated_at::date AS dia, COUNT(DISTINCT wa_phone) AS usuarios
           FROM wa_booking_state
           WHERE updated_at >= NOW() - INTERVAL '14 days'
           GROUP BY dia ORDER BY dia""",
        fetch_all=True,
    )
    # Recent users (last 20)
    recent = execute(
        """SELECT wa_phone,
                  data->>'customer_name' AS nombre,
                  state,
                  updated_at
           FROM wa_booking_state
           ORDER BY updated_at DESC
           LIMIT 20""",
        fetch_all=True,
    )

    n_total = total_users["cnt"] if total_users else 0
    n_7d = active_7d["cnt"] if active_7d else 0
    n_today = active_today["cnt"] if active_today else 0

    # Build chart data
    chart_labels = []
    chart_values = []
    for row in (daily_activity or []):
        chart_labels.append(str(row["dia"])[5:])  # MM-DD
        chart_values.append(row["usuarios"])

    # Build table rows
    table_rows = ""
    state_labels = {
        "idle": ("Inactivo", "#6b7280"),
        "choosing_date": ("Eligiendo fecha", "#f59e0b"),
        "choosing_time": ("Eligiendo hora", "#f59e0b"),
        "choosing_court": ("Eligiendo cancha", "#f59e0b"),
        "confirming": ("Confirmando", "#3b82f6"),
        "choosing_payment": ("Pagando", "#8b5cf6"),
    }
    for r in (recent or []):
        phone = r["wa_phone"][-4:]
        name = r.get("nombre") or "—"
        st = r["state"]
        label, color = state_labels.get(st, (st, "#6b7280"))
        ts = str(r["updated_at"])[:16]
        table_rows += f"""
        <tr>
          <td>{name}</td>
          <td>***{phone}</td>
          <td><span class="badge" style="background:{color}">{label}</span></td>
          <td>{ts}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MatchBot - Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#0f172a; color:#e2e8f0; min-height:100vh; }}
  .header {{ background:linear-gradient(135deg,#1e3a5f,#0f172a); padding:24px 32px; border-bottom:1px solid #1e293b; }}
  .header h1 {{ font-size:24px; color:#fff; }}
  .header p {{ color:#94a3b8; font-size:14px; margin-top:4px; }}
  .container {{ max-width:960px; margin:0 auto; padding:24px 16px; }}
  .cards {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-bottom:32px; }}
  .card {{ background:#1e293b; border-radius:12px; padding:24px; text-align:center; border:1px solid #334155; }}
  .card .number {{ font-size:42px; font-weight:700; }}
  .card .label {{ font-size:13px; color:#94a3b8; margin-top:6px; text-transform:uppercase; letter-spacing:1px; }}
  .card.green .number {{ color:#22c55e; }}
  .card.blue .number {{ color:#3b82f6; }}
  .card.yellow .number {{ color:#f59e0b; }}
  .section {{ background:#1e293b; border-radius:12px; padding:24px; margin-bottom:24px; border:1px solid #334155; }}
  .section h2 {{ font-size:16px; color:#fff; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ text-align:left; font-size:12px; color:#64748b; text-transform:uppercase; letter-spacing:1px; padding:8px 12px; border-bottom:1px solid #334155; }}
  td {{ padding:10px 12px; border-bottom:1px solid #1e293b; font-size:14px; }}
  tr:hover {{ background:#334155; }}
  .badge {{ padding:3px 10px; border-radius:12px; font-size:11px; color:#fff; font-weight:500; }}
  canvas {{ max-height:220px; }}
  @media(max-width:600px) {{
    .cards {{ grid-template-columns:1fr; }}
    .card .number {{ font-size:32px; }}
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>MatchBot Dashboard</h1>
    <p>Club de Padel Victoria</p>
  </div>
  <div class="container">
    <div class="cards">
      <div class="card green">
        <div class="number">{n_total}</div>
        <div class="label">Usuarios totales</div>
      </div>
      <div class="card blue">
        <div class="number">{n_7d}</div>
        <div class="label">Activos (7 dias)</div>
      </div>
      <div class="card yellow">
        <div class="number">{n_today}</div>
        <div class="label">Activos hoy</div>
      </div>
    </div>
    <div class="section">
      <h2>Actividad diaria (ultimos 14 dias)</h2>
      <canvas id="chart"></canvas>
    </div>
    <div class="section">
      <h2>Usuarios recientes</h2>
      <table>
        <thead><tr><th>Nombre</th><th>Tel</th><th>Estado</th><th>Ultima actividad</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>
  <script>
    new Chart(document.getElementById('chart'), {{
      type: 'bar',
      data: {{
        labels: {json.dumps(chart_labels)},
        datasets: [{{
          label: 'Usuarios activos',
          data: {json.dumps(chart_values)},
          backgroundColor: 'rgba(34,197,94,0.6)',
          borderColor: '#22c55e',
          borderWidth: 1,
          borderRadius: 6,
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          y: {{ beginAtZero: true, ticks: {{ stepSize: 1, color: '#64748b' }}, grid: {{ color: '#1e293b' }} }},
          x: {{ ticks: {{ color: '#64748b' }}, grid: {{ display: false }} }}
        }}
      }}
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    return {"status": "ok", "app": "MatchBot", "version": "1.0.0"}
