"""
Playtomic API Client for MatchBot
Handles: authentication, availability queries, booking creation.

Auth flow (reverse-engineered from manager.playtomic.io):
  1. POST /v3/auth/login  → customer access_token + refresh_token
  2. POST /v3/auth/token  → tenant-scoped access_token (needed for bookings)
  3. POST /v1/matches      → creates booking with tenant token
"""

import httpx
import os
import re
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
        self.token: Optional[str] = None          # customer token
        self.tenant_token: Optional[str] = None    # tenant-scoped token (for bookings)
        self.refresh_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.client = httpx.AsyncClient(timeout=15.0)

    # ─── AUTH ───
    async def login(self) -> bool:
        """Step 1: Authenticate with email/password → customer token + refresh token."""
        if not PLAYTOMIC_EMAIL or not PLAYTOMIC_PASSWORD:
            logger.error("PLAYTOMIC_EMAIL / PLAYTOMIC_PASSWORD not set")
            return False

        try:
            r = await self.client.post(
                f"{PLAYTOMIC_API}/v3/auth/login",
                json={"email": PLAYTOMIC_EMAIL, "password": PLAYTOMIC_PASSWORD},
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

    async def get_tenant_token(self) -> bool:
        """Step 2: Exchange refresh_token for a tenant-scoped access token.
        This is required for manager operations (creating bookings).
        """
        if not self.refresh_token:
            logger.error("No refresh token available for tenant token exchange")
            return False

        try:
            r = await self.client.post(
                f"{PLAYTOMIC_API}/v3/auth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "scope": f"tenant:{TENANT_ID}",
                },
            )
            if r.status_code == 200:
                data = r.json()
                self.tenant_token = data.get("access_token")
                # The exchange may return a new refresh token
                new_rt = data.get("refresh_token")
                if new_rt:
                    self.refresh_token = new_rt
                logger.info("Tenant token obtained OK")
                return True
            else:
                logger.error(f"Tenant token exchange failed: {r.status_code} {r.text}")
                return False
        except Exception as e:
            logger.error(f"Tenant token exchange error: {e}")
            return False

    async def ensure_auth(self):
        """Ensure we have a valid customer token."""
        if not self.token:
            await self.login()

    async def ensure_tenant_auth(self):
        """Ensure we have a valid tenant-scoped token for bookings."""
        if not self.token:
            await self.login()
        if not self.tenant_token:
            await self.get_tenant_token()

    # ─── AVAILABILITY (public, no auth needed) ───
    async def get_availability(self, date_str: str) -> list:
        """
        Get court availability for a given date.
        date_str: 'YYYY-MM-DD'
        Returns list of available slots grouped by court.
        """
        try:
            # Use local_start_min/max so Playtomic interprets as club timezone
            params = {
                "user_id": "me",
                "sport_id": "PADEL",
                "tenant_id": TENANT_ID,
                "local_start_min": f"{date_str}T00:00:00",
                "local_start_max": f"{date_str}T23:59:59",
            }
            logger.info(f"Querying availability: {params}")
            r = await self.client.get(
                f"{PLAYTOMIC_API}/v1/availability",
                params=params,
            )
            if r.status_code == 200:
                data = r.json()
                # Log raw response summary for debugging
                if isinstance(data, list):
                    logger.info(f"Availability response: {len(data)} resources")
                    for res in data:
                        slots = res.get("slots", [])
                        name = res.get("resource_name", res.get("name", res.get("resource_id", "?")))
                        logger.info(f"  {name}: {len(slots)} slots")
                else:
                    logger.info(f"Availability raw (not list): {str(data)[:500]}")
                return self._parse_availability(data)
            else:
                logger.error(f"Availability error: {r.status_code} {r.text[:500]}")
                return []
        except Exception as e:
            logger.error(f"Availability error: {e}")
            return []

    def _parse_availability(self, data: list) -> list:
        """Parse raw Playtomic availability response.
        Handles multiple response formats from Playtomic API:

        Format A (slots array):
        [{"resource_id": "uuid", "start_date": "2026-07-05",
          "slots": [{"start_time": "21:00:00", "duration": 90, "price": "300 MXN"}]}]

        Format B (flat per-slot):
        [{"resource_id": "uuid", "resource_name": "Cancha 1",
          "start_date": "2026-07-05", "start_time": "21:00:00",
          "duration": 90, "price": 300}]
        """
        if not data or not isinstance(data, list):
            logger.warning(f"Unexpected availability data type: {type(data)}")
            return []

        # Detect format: if first item has "slots" array, it's Format A
        # If first item has "start_time" at top level, it's Format B (flat)
        sample = data[0] if data else {}

        if "slots" in sample:
            return self._parse_format_slots(data)
        elif "start_time" in sample or "start" in sample:
            return self._parse_format_flat(data)
        else:
            # Unknown format — log it
            logger.warning(f"Unknown availability format. Keys: {list(sample.keys())}")
            logger.warning(f"Sample entry: {str(sample)[:500]}")
            # Try Format A as fallback
            return self._parse_format_slots(data)

    def _parse_format_slots(self, data: list) -> list:
        """Parse Format A: each resource has a 'slots' array."""
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
                parsed = self._parse_slot(slot, resource_id, start_date)
                if parsed:
                    slots.append(parsed)

            if slots:
                results.append({
                    "resource_id": resource_id,
                    "name": resource_name,
                    "slots": slots,
                })

        return results

    def _parse_format_flat(self, data: list) -> list:
        """Parse Format B: flat list where each item is one slot."""
        # Group by resource_id
        by_resource = {}
        for item in data:
            resource_id = item.get("resource_id", "")
            resource_name = (
                item.get("resource_name")
                or item.get("name")
                or ""
            )
            start_date = item.get("start_date", item.get("date", ""))

            parsed = self._parse_slot(item, resource_id, start_date)
            if parsed:
                if resource_id not in by_resource:
                    by_resource[resource_id] = {
                        "resource_id": resource_id,
                        "name": resource_name or f"Cancha {len(by_resource) + 1}",
                        "slots": [],
                    }
                by_resource[resource_id]["slots"].append(parsed)

        return list(by_resource.values())

    def _parse_slot(self, slot: dict, resource_id: str, start_date: str) -> dict | None:
        """Parse a single slot from any format."""
        raw_time = (
            slot.get("start_time")
            or slot.get("start", "")
        )
        if not raw_time:
            return None

        # If raw_time is a full ISO datetime, extract date and time
        if "T" in raw_time:
            parts = raw_time.split("T")
            start_date = parts[0]
            raw_time = parts[1]

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

        return {
            "start": start_iso,
            "time": time_str,
            "duration": duration,
            "price": price,
            "resource_id": resource_id,
        }

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

    # ─── BOOKING (requires tenant-scoped auth) ───
    async def create_booking(
        self,
        resource_id: str,
        start_time: str,
        duration: int = 90,
        customer_name: str = "",
        customer_phone: str = "",
    ) -> dict:
        """
        Create a booking on Playtomic as the club manager.
        Uses POST /v1/matches with tenant-scoped token.
        This is the same API the manager.playtomic.io dashboard uses.
        """
        await self.ensure_tenant_auth()
        if not self.tenant_token:
            return {"error": "No se pudo obtener token de manager Playtomic"}

        headers = {
            "Authorization": f"Bearer {self.tenant_token}",
            "Content-Type": "application/json",
        }

        # Calculate end_time from start + duration
        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = start_dt + timedelta(minutes=duration)
            end_time = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            end_time = start_time  # fallback

        # Build player entry for Team A
        player = {}
        if customer_name:
            player["name"] = customer_name
        if customer_phone:
            # Playtomic expects phone without country code prefix in some cases
            player["phone"] = customer_phone

        teams = [
            {
                "team_id": "0",
                "players": [player] if player else [],
            },
            {
                "team_id": "1",
                "players": [],
            },
        ]

        match_payload = {
            "sport_id": "PADEL",
            "tenant_id": TENANT_ID,
            "resource_id": resource_id,
            "start_date": start_time,
            "end_date": end_time,
            "match_type": "BOOKING",
            "match_organization": "TENANT",
            "visibility": "HIDDEN",
            "competition_mode": "COMPETITIVE",
            "min_players_per_team": 2,
            "max_players_per_team": 2,
            "teams": teams,
        }

        try:
            logger.info(f"Creating Playtomic booking: {resource_id} at {start_time} for {customer_name}")
            r = await self.client.post(
                f"{PLAYTOMIC_API}/v1/matches",
                headers=headers,
                json=match_payload,
            )

            if r.status_code == 401:
                # Tenant token expired — refresh and retry once
                logger.info("Tenant token expired, refreshing...")
                self.tenant_token = None
                await self.login()
                await self.get_tenant_token()
                if self.tenant_token:
                    headers["Authorization"] = f"Bearer {self.tenant_token}"
                    r = await self.client.post(
                        f"{PLAYTOMIC_API}/v1/matches",
                        headers=headers,
                        json=match_payload,
                    )
                else:
                    return {"error": "No se pudo renovar el token de manager"}

            logger.info(f"Create booking response: {r.status_code} {r.text[:500]}")

            if r.status_code in (200, 201):
                match_data = r.json()
                match_id = match_data.get("match_id", "")
                logger.info(f"Booking created OK — match_id: {match_id}")
                return {"success": True, "booking": match_data, "match_id": match_id}
            else:
                logger.error(f"Booking failed: {r.status_code} {r.text[:300]}")
                return {"error": f"Error al crear reserva: {r.status_code} - {r.text[:200]}"}

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
