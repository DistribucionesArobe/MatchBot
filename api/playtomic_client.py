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
        """Parse raw Playtomic availability response.
        Actual format from API:
        [{"resource_id": "uuid", "start_date": "2026-07-05",
          "slots": [{"start_time": "21:00:00", "duration": 90, "price": "300 MXN"}]}]
        """
        import re
        results = []
        for i, resource in enumerate(data):
            resource_id = resource.get("resource_id", "")
            resource_name = (
                resource.get("resource_name")
                or resource.get("name")
                or f"Cancha {i + 1}"
            )
            start_date = resource.get("start_date", "")

            slots = []
            for slot in resource.get("slots", []):
                raw_time = slot.get("start_time") or slot.get("start", "")

                # Duration → int
                try:
                    duration = int(slot.get("duration", 90))
                except (ValueError, TypeError):
                    duration = 90

                # Price: "300 MXN" → 300.0, or 300 → 300.0, or {"amount":300}
                raw_price = slot.get("price", 0)
                if isinstance(raw_price, dict):
                    price = float(raw_price.get("amount", 0))
                elif isinstance(raw_price, str):
                    match = re.search(r'[\d.]+', raw_price)
                    price = float(match.group()) if match else 0.0
                else:
                    try:
                        price = float(raw_price)
                    except (ValueError, TypeError):
                        price = 0.0

                # Display time: "21:00:00" → "21:00"
                time_str = raw_time[:5] if len(raw_time) >= 5 else raw_time

                # Full ISO datetime for booking: "2026-07-05T21:00:00"
                start_iso = f"{start_date}T{raw_time}" if start_date else raw_time

                if raw_time:
                    slots.append({
                        "start": start_iso,
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
        Flow: POST /v1/matches → POST /v1/payment_intents → PATCH → confirm.
        """
        await self.ensure_auth()
        if not self.token:
            return {"error": "No se pudo autenticar con Playtomic"}

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        try:
            # Step 1: Create match
            match_payload = {
                "tenant_id": TENANT_ID,
                "resource_id": resource_id,
                "sport_id": "PADEL",
                "start": start_time,
                "duration": duration,
                "number_of_players": 4,
                "match_registrations": [
                    {"user_id": self.user_id, "pay_now": False}
                ],
            }

            logger.info(f"Creating match for {resource_id} at {start_time}")
            r = await self.client.post(
                f"{PLAYTOMIC_API}/v1/matches",
                headers=headers,
                json=match_payload,
            )

            if r.status_code == 401:
                logger.info("Token expired, re-authenticating...")
                await self.login()
                return await self.create_booking(
                    resource_id, start_time, duration,
                    customer_name, customer_phone,
                )

            logger.info(f"Create match response: {r.status_code} {r.text[:500]}")

            if r.status_code not in (200, 201):
                return {"error": f"Error al crear match: {r.status_code} - {r.text[:200]}"}

            match_data = r.json()
            match_id = match_data.get("match_id") or match_data.get("id", "")
            logger.info(f"Match created: {match_id}")

            if not match_id:
                # Maybe the match creation itself completes the booking
                return {"success": True, "booking": match_data}

            # Step 2: Create payment intent for this match
            intent_payload = {
                "allowed_payment_method_types": ["OFFER"],
                "user_id": self.user_id,
                "cart": {
                    "requested_item": {
                        "cart_item_type": "MATCH",
                        "cart_item_voucher_id": None,
                        "cart_item_data": {
                            "supports_split_payment": False,
                            "number_of_players": 4,
                            "tenant_id": TENANT_ID,
                            "resource_id": resource_id,
                            "start": start_time,
                            "duration": duration,
                            "match_id": match_id,
                            "match_registrations": [
                                {"user_id": self.user_id, "pay_now": False, "match_id": match_id}
                            ],
                        }
                    }
                }
            }

            r2 = await self.client.post(
                f"{PLAYTOMIC_API}/v1/payment_intents",
                headers=headers,
                json=intent_payload,
            )
            logger.info(f"Payment intent response: {r2.status_code} {r2.text[:500]}")

            if r2.status_code not in (200, 201):
                # Match was created but payment failed — might still be ok
                return {"success": True, "booking": match_data, "note": "Match created, payment pending"}

            intent_data = r2.json()
            intent_id = intent_data.get("payment_intent_id", "")

            if not intent_id:
                return {"success": True, "booking": match_data}

            # Step 3: Select payment method
            available_methods = intent_data.get("available_payment_methods", [])
            selected_method = "OFFER"
            if available_methods:
                for m in available_methods:
                    mt = m if isinstance(m, str) else m.get("payment_method_type", "")
                    if mt in ("OFFER", "CASH", "FREE", "IN_PERSON"):
                        selected_method = mt
                        break

            await self.client.patch(
                f"{PLAYTOMIC_API}/v1/payment_intents/{intent_id}",
                headers=headers,
                json={"selected_payment_method": selected_method},
            )

            # Step 4: Confirm
            r4 = await self.client.post(
                f"{PLAYTOMIC_API}/v1/payment_intents/{intent_id}/confirmation",
                headers=headers,
            )
            logger.info(f"Confirmation response: {r4.status_code} {r4.text[:300]}")

            return {"success": True, "booking": match_data, "payment_intent_id": intent_id}

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
