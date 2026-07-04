"""
Playtomic API Client for MatchBot
Handles: authentication, availability queries, booking creation
Based on reverse-engineered API from community projects.
"""

import httpx
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Config ───
PLAYTOMIC_API = "https://api.playtomic.io"
TENANT_ID = os.getenv("PLAYTOMIC_TENANT_ID", "9350708e-5320-4e4c-a264-0f6aedefaf8b")
PLAYTOMIC_EMAIL = os.getenv("PLAYTOMIC_EMAIL", "")
PLAYTOMIC_PASSWORD = os.getenv("PLAYTOMIC_PASSWORD", "")


class PlaytomicClient:
    def __init__(self):
        self.token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.client = httpx.AsyncClient(timeout=15.0)

    # ─── AUTH ───
    async def login(self) -> bool:
        """Authenticate with Playtomic and get access token."""
        if not PLAYTOMIC_EMAIL or not PLAYTOMIC_PASSWORD:
            logger.error("PLAYTOMIC_EMAIL / PLAYTOMIC_PASSWORD not set")
            return False

        try:
            r = await self.client.post(
                f"{PLAYTOMIC_API}/v3/auth/login",
                json={
                    "email": PLAYTOMIC_EMAIL,
                    "password": PLAYTOMIC_PASSWORD,
                },
            )
            if r.status_code == 200:
                data = r.json()
                self.token = data.get("access_token")
                self.refresh_token = data.get("refresh_token")
                self.user_id = data.get("user_id")
                logger.info(f"Playtomic login OK — user_id: {self.user_id}")
                return True
            else:
                logger.error(f"Playtomic login failed: {r.status_code} {r.text}")
                return False
        except Exception as e:
            logger.error(f"Playtomic login error: {e}")
            return False

    async def ensure_auth(self):
        """Ensure we have a valid token, login if needed."""
        if not self.token:
            await self.login()

    # ─── AVAILABILITY (public, no auth needed) ───
    async def get_availability(self, date_str: str) -> list:
        """
        Get court availability for a given date.
        date_str: 'YYYY-MM-DD'
        Returns list of available slots grouped by court.
        """
        try:
            r = await self.client.get(
                f"{PLAYTOMIC_API}/v1/availability",
                params={
                    "sport_id": "PADEL",
                    "tenant_id": TENANT_ID,
                    "start_min": f"{date_str}T00:00:00",
                    "start_max": f"{date_str}T23:59:59",
                },
            )
            if r.status_code == 200:
                data = r.json()
                return self._parse_availability(data)
            else:
                logger.error(f"Availability error: {r.status_code} {r.text}")
                return []
        except Exception as e:
            logger.error(f"Availability error: {e}")
            return []

    def _parse_availability(self, data: list) -> list:
        """Parse raw availability into friendly format."""
        results = []
        for resource in data:
            resource_id = resource.get("resource_id", "")
            resource_name = resource.get("resource_name") or resource.get("name") or f"Cancha {resource_id}"

            slots = []
            for slot in resource.get("slots", []):
                start = slot.get("start_time") or slot.get("start", "")
                try:
                    duration = int(slot.get("duration", 90))
                except (ValueError, TypeError):
                    duration = 90
                price = slot.get("price", 0)

                # Parse time for display
                # Normalize price to float
                if isinstance(price, dict):
                    price = float(price.get("amount", 0))
                else:
                    try:
                        price = float(price)
                    except (ValueError, TypeError):
                        price = 0.0

                if start:
                    try:
                        t = datetime.fromisoformat(start.replace("Z", "+00:00"))
                        time_str = t.strftime("%H:%M")
                    except:
                        time_str = start

                    slots.append({
                        "start": start,
                        "time": time_str,
                        "duration": duration,
                        "price": price,
                        "resource_id": resource_id,
                    })

            if slots:
                results.append({
                    "resource_id": resource_id,
                    "name": resource_name,
                    "slots": slots,
                })

        return results

    def format_availability_whatsapp(self, availability: list, date_str: str) -> str:
        """Format availability data as a WhatsApp-friendly message."""
        if not availability:
            return f"No hay canchas disponibles para el {date_str}."

        lines = [f"🎾 *Canchas disponibles — {date_str}*\n"]

        for i, court in enumerate(availability):
            lines.append(f"*{court['name']}*")
            slot_texts = []
            for slot in court["slots"]:
                price_str = f"${slot['price']}" if slot['price'] else ""
                slot_texts.append(f"  {slot['time']} ({slot['duration']}min) {price_str}")
            lines.append("\n".join(slot_texts))
            lines.append("")

        lines.append("Responde con el número de cancha y la hora.")
        lines.append("Ejemplo: *Cancha 1 18:00*")

        return "\n".join(lines)

    # ─── BOOKING (requires auth) ───
    async def create_booking(
        self,
        resource_id: str,
        start_time: str,
        duration: int = 90,
        customer_name: str = "",
        customer_phone: str = "",
    ) -> dict:
        """
        Create a booking on Playtomic.
        resource_id: court UUID from availability
        start_time: ISO format datetime string
        duration: minutes (60, 90, 120)
        Returns dict with booking info or error.
        """
        await self.ensure_auth()
        if not self.token:
            return {"error": "No se pudo autenticar con Playtomic"}

        try:
            # Try the known booking endpoint
            r = await self.client.post(
                f"{PLAYTOMIC_API}/v1/tenants/{TENANT_ID}/bookings",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json={
                    "resource_id": resource_id,
                    "start_time": start_time,
                    "duration": duration,
                    "sport_id": "PADEL",
                    "customer": {
                        "name": customer_name,
                        "phone": customer_phone,
                    },
                },
            )

            if r.status_code in (200, 201):
                data = r.json()
                logger.info(f"Booking created: {data.get('id', 'ok')}")
                return {"success": True, "booking": data}
            elif r.status_code == 401:
                # Token expired, retry
                logger.info("Token expired, re-authenticating...")
                await self.login()
                return await self.create_booking(
                    resource_id, start_time, duration,
                    customer_name, customer_phone,
                )
            else:
                logger.error(f"Booking error: {r.status_code} {r.text}")
                return {"error": f"Error al reservar: {r.status_code}"}
        except Exception as e:
            logger.error(f"Booking exception: {e}")
            return {"error": f"Error de conexión: {e}"}

    # ─── TENANT INFO ───
    async def get_tenant_info(self) -> dict:
        """Get club info from Playtomic."""
        try:
            r = await self.client.get(
                f"{PLAYTOMIC_API}/v1/tenants/{TENANT_ID}"
            )
            if r.status_code == 200:
                return r.json()
            return {}
        except:
            return {}


# Singleton
playtomic = PlaytomicClient()
