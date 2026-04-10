"""
MatchBot - WhatsApp Message Sender (Meta Cloud API)
Adapted from CotizaExpress pattern.
"""
import httpx
import logging
from config.settings import settings

logger = logging.getLogger("matchbot.wa")


async def send_text(phone_id: str, token: str, to: str, body: str):
    """Send a plain text message."""
    await _send(phone_id, token, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    })


async def send_interactive_buttons(
    phone_id: str, token: str, to: str,
    body: str, buttons: list[dict], header: str = None, footer: str = None
):
    """
    Send interactive button message (max 3 buttons).
    buttons = [{"id": "btn_1", "title": "Reservar"}]
    """
    action = {"buttons": [
        {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
        for b in buttons[:3]
    ]}
    msg = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": action,
        }
    }
    if header:
        msg["interactive"]["header"] = {"type": "text", "text": header}
    if footer:
        msg["interactive"]["footer"] = {"text": footer}

    await _send(phone_id, token, msg)


async def send_interactive_list(
    phone_id: str, token: str, to: str,
    body: str, button_text: str, sections: list[dict],
    header: str = None, footer: str = None
):
    """
    Send interactive list message.
    sections = [{"title": "Mañana", "rows": [{"id": "slot_1", "title": "09:00 - 10:30", "description": "Cancha 1 - $350"}]}]
    """
    msg = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text[:20],
                "sections": sections,
            }
        }
    }
    if header:
        msg["interactive"]["header"] = {"type": "text", "text": header}
    if footer:
        msg["interactive"]["footer"] = {"text": footer}

    await _send(phone_id, token, msg)


async def _send(phone_id: str, token: str, payload: dict):
    """Internal: POST to WhatsApp Cloud API."""
    url = f"{settings.wa_api_url}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            logger.error(f"WA API error {resp.status_code}: {resp.text}")
            resp.raise_for_status()
        return resp.json()
