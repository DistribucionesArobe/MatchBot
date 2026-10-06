"""
MatchBot — Promociones masivas por WhatsApp (estilo email marketing).

Reglas de WhatsApp/Meta:
  - Fuera de la ventana de 24h solo se pueden enviar PLANTILLAS
    aprobadas por Meta (categoría MARKETING).
  - Todo mensaje promocional permite darse de baja (responder BAJA).
  - El envío se hace espaciado para cuidar la calidad del número.

Uso (solo el dueño, desde CLUB_NOTIFY_PHONE):
  promo: 2x1 en canchas techadas este jueves
"""
import os
import asyncio
import logging

import httpx

from db.database import execute

logger = logging.getLogger("matchbot.promos")

TEMPLATE_NAME = "promo_club"
TEMPLATE_LANG = "es_MX"
GRAPH = "https://graph.facebook.com/v25.0"


# ─── Opt-out ─────────────────────────────────────────

def ensure_tables():
    execute("""
        CREATE TABLE IF NOT EXISTS promo_optout (
            wa_phone TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)


def opt_out(wa_phone: str):
    try:
        execute(
            "INSERT INTO promo_optout (wa_phone) VALUES (%s) ON CONFLICT DO NOTHING",
            [wa_phone],
        )
    except Exception as e:
        logger.error(f"opt_out failed: {e}")


def opted_out_set() -> set:
    try:
        rows = execute("SELECT wa_phone FROM promo_optout", fetch_all=True) or []
        return {r["wa_phone"] for r in rows}
    except Exception:
        return set()


def all_customer_phones(exclude: set = None) -> list[str]:
    """Every phone that has talked to the bot, minus opt-outs/excluded."""
    phones = set()
    try:
        for r in execute("SELECT DISTINCT wa_phone FROM wa_booking_state", fetch_all=True) or []:
            phones.add(r["wa_phone"])
    except Exception as e:
        logger.error(f"phones query failed: {e}")
    try:
        for r in execute("SELECT DISTINCT wa_phone FROM loyalty_bookings", fetch_all=True) or []:
            phones.add(r["wa_phone"])
    except Exception:
        pass
    skip = opted_out_set() | (exclude or set())
    return sorted(p for p in phones if p and p not in skip)


# ─── Creación de la plantilla vía API de Meta ────────

async def discover_waba_id(token: str) -> str | None:
    """Find the WhatsApp Business Account id for this token."""
    env_waba = os.getenv("WABA_ID", "")
    if env_waba:
        return env_waba
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{GRAPH}/debug_token",
                params={"input_token": token, "access_token": token},
            )
            if r.status_code == 200:
                data = r.json().get("data", {})
                for gs in data.get("granular_scopes", []):
                    if gs.get("scope") in ("whatsapp_business_management",
                                           "whatsapp_business_messaging"):
                        ids = gs.get("target_ids") or []
                        if ids:
                            return str(ids[0])
    except Exception as e:
        logger.error(f"discover_waba_id failed: {e}")
    return None


async def create_promo_template(token: str) -> dict:
    """Create the 'promo_club' MARKETING template via the Graph API."""
    waba = await discover_waba_id(token)
    if not waba:
        return {"ok": False, "error": "No pude descubrir el WABA ID. Agrega la env var WABA_ID en Render (está en Meta Business Manager → WhatsApp → Configuración)."}

    payload = {
        "name": TEMPLATE_NAME,
        "language": TEMPLATE_LANG,
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": ("🎾 *Club de Padel Victoria*\n\n{{1}}\n\n"
                         "📲 Reserva por este WhatsApp: escribe *Reservar*."),
                "example": {"body_text": [["2x1 en canchas techadas este jueves"]]},
            },
            {
                "type": "FOOTER",
                "text": "Responde BAJA para no recibir promociones",
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{GRAPH}/{waba}/message_templates",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            body = r.json() if r.text else {}
            if r.status_code in (200, 201):
                return {"ok": True, "waba_id": waba, "response": body,
                        "nota": "Plantilla enviada a revisión de Meta (suele aprobarse en minutos/horas)."}
            return {"ok": False, "waba_id": waba, "status": r.status_code, "response": body}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


async def template_status(token: str) -> dict:
    """Check approval status of the promo template."""
    waba = await discover_waba_id(token)
    if not waba:
        return {"ok": False, "error": "WABA ID no disponible"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{GRAPH}/{waba}/message_templates",
                headers={"Authorization": f"Bearer {token}"},
                params={"name": TEMPLATE_NAME},
            )
            if r.status_code == 200:
                for t in r.json().get("data", []):
                    if t.get("name") == TEMPLATE_NAME:
                        return {"ok": True, "status": t.get("status"),
                                "language": t.get("language")}
                return {"ok": True, "status": "NOT_FOUND"}
            return {"ok": False, "status_code": r.status_code, "body": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ─── Envío masivo ────────────────────────────────────

async def broadcast(phone_id: str, token: str, promo_text: str,
                    owner_phone: str) -> dict:
    """Send the promo template to every customer (spaced out)."""
    from whatsapp.sender import send_template, send_text

    phones = all_customer_phones(exclude={owner_phone})
    sent, failed = 0, 0
    for p in phones:
        try:
            await send_template(phone_id, token, p, TEMPLATE_NAME,
                                [promo_text], TEMPLATE_LANG)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"promo to {p} failed: {e}")
        await asyncio.sleep(0.6)  # pace to protect number quality

    # Report back to the owner
    try:
        await send_text(phone_id, token, owner_phone,
            f"📣 *Promo enviada*\n\n"
            f"✅ Entregada a {sent} clientes\n"
            f"{'⚠️ Fallaron ' + str(failed) if failed else '✔️ Sin fallos'}\n\n"
            f"Texto: {promo_text}")
    except Exception:
        pass
    return {"sent": sent, "failed": failed}
