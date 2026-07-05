"""
Playtomic API Client for MatchBot
Handles: authentication, availability queries, booking creation.

Auth flow (reverse-engineered from manager.playtomic.io):
  1. POST /v3/auth/login  → customer access_token + refresh_token
  2. POST /v3/auth/token  → tenant-scoped access_token (needed for bookings)
  3. POST /v1/matches      → creates booking with tenant token

IMPORTANT: Playtomic API returns times in UTC.
  Cd. Victoria (Tamaulipas) = UTC-6 (CST).
  We convert UTC→local for display and extend query range to cover full local day.
"""

import httpx
import os
import re
import logging
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Config ───
PLAYTOMIC_API = "https://api.playtomic.io"
TENANT_ID = os.getenv("PLAYTOMIC_TENANT_ID", "9350708e-5320-4e4c-a264-0f6aedefaf8b")
PLAYTOMIC_EMAIL = os.getenv("PLAYTOMIC_EMAIL", "")
PLAYTOMIC_PASSWORD = os.getenv("PLAYTOMIC_PASSWORD", "")

# Club timezone offset from UTC (Cd. Victoria = -6)
CLUB_UTC_OFFSET = int(os.getenv("CLUB_UTC_OFFSET", "-6"))


class PlaytomicClient:
    def __init__(self):
        self.token: Optional[str] = None          # customer token
        self.tenant_token: Optional[str] = None    # tenant-scoped token (for bookings)
        self.refresh_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.client = httpx.AsyncClient(timeout=15.0)
        self._resource_names: dict[str, str] = {}  # resource_id → display name cache

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

    # ─── RESOURCE NAMES (court names from tenant info) ───
    async def _ensure_resource_names(self):
        """Load court/resource names from Playtomic tenant info and cache them."""
        if self._resource_names:
            return  # already loaded

        try:
            r = await self.client.get(f"{PLAYTOMIC_API}/v1/tenants/{TENANT_ID}")
            if r.status_code == 200:
                info = r.json()
                # Playtomic tenant info includes resources array
                resources = info.get("resources", [])
                for res in resources:
                    rid = res.get("resource_id", res.get("id", ""))
                    name = res.get("name", "")
                    if rid and name:
                        self._resource_names[rid] = name
                logger.info(f"Loaded {len(self._resource_names)} resource names: {list(self._resource_names.values())}")
            else:
                logger.warning(f"Could not load tenant info: {r.status_code}")
        except Exception as e:
            logger.warning(f"Could not load resource names: {e}")

    def _get_resource_name(self, resource_id: str, fallback_index: int) -> str:
        """Get display name for a resource, with fallback."""
        if resource_id in self._resource_names:
            return self._resource_names[resource_id]
        return f"Cancha {fallback_index + 1}"

    # ─── TIMEZONE HELPERS ───
    @staticmethod
    def _utc_to_local_hour(utc_hour: int, utc_minute: int = 0) -> tuple[int, int, int]:
        """Convert UTC hour:minute to local. Returns (local_hour, local_minute, day_offset).
        day_offset: -1 = previous day, 0 = same day, +1 = next day.
        """
        local_h = utc_hour + CLUB_UTC_OFFSET
        day_offset = 0
        if local_h < 0:
            local_h += 24
            day_offset = -1
        elif local_h >= 24:
            local_h -= 24
            day_offset = 1
        return local_h, utc_minute, day_offset

    @staticmethod
    def _local_time_str(utc_time_str: str) -> str:
        """Convert 'HH:MM' or 'HH:MM:SS' UTC to 'HH:MM' local."""
        parts = utc_time_str.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        local_h, local_m, _ = PlaytomicClient._utc_to_local_hour(h, m)
        return f"{local_h:02d}:{local_m:02d}"

    @staticmethod
    def _local_date_for_utc(utc_date_str: str, utc_time_str: str) -> str:
        """Get the LOCAL date for a UTC datetime, accounting for day shift."""
        parts = utc_time_str.split(":")
        h = int(parts[0])
        _, _, day_offset = PlaytomicClient._utc_to_local_hour(h)
        d = date.fromisoformat(utc_date_str)
        if day_offset != 0:
            d = d + timedelta(days=day_offset)
        return d.isoformat()

    # ─── AVAILABILITY (public, no auth needed) ───
    async def get_availability(self, date_str: str) -> list:
        """
        Get court availability for a given date (local).
        date_str: 'YYYY-MM-DD' in LOCAL timezone.
        Returns list of available slots grouped by court, with LOCAL times.
        """
        # Load court names first
        await self._ensure_resource_names()

        # Calculate UTC range to cover the full LOCAL day
        # For UTC-6: local midnight = 06:00 UTC, local 23:59 = next day 05:59 UTC
        offset_h = abs(CLUB_UTC_OFFSET)
        target_date = date.fromisoformat(date_str)
        next_day = (target_date + timedelta(days=1)).isoformat()

        utc_start = f"{date_str}T{offset_h:02d}:00:00"
        utc_end = f"{next_day}T{offset_h:02d}:00:00"

        try:
            params = {
                "user_id": "me",
                "sport_id": "PADEL",
                "tenant_id": TENANT_ID,
                "local_start_min": f"{date_str}T00:00:00",
                "local_start_max": f"{date_str}T23:59:59",
                "start_min": utc_start,
                "start_max": utc_end,
            }
            logger.info(f"Querying availability: date={date_str}, UTC range={utc_start} to {utc_end}")
            r = await self.client.get(
                f"{PLAYTOMIC_API}/v1/availability",
                params=params,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    logger.info(f"Availability: {len(data)} items returned")
                    if data:
                        logger.info(f"Sample keys: {list(data[0].keys())}")
                else:
                    logger.warning(f"Unexpected response type: {type(data)}")
                    logger.warning(f"Response: {str(data)[:500]}")
                return self._parse_availability(data, date_str)
            else:
                logger.error(f"Availability error: {r.status_code} {r.text[:500]}")
                return []
        except Exception as e:
            logger.error(f"Availability error: {e}")
            return []

    def _parse_availability(self, data: list, target_local_date: str) -> list:
        """Parse raw Playtomic availability response.
        Converts UTC times to local and filters to target date.
        """
        if not data or not isinstance(data, list):
            return []

        sample = data[0] if data else {}

        # Detect format
        if "slots" in sample:
            return self._parse_format_slots(data, target_local_date)
        elif "start_time" in sample or "start" in sample:
            return self._parse_format_flat(data, target_local_date)
        else:
            logger.warning(f"Unknown format. Keys: {list(sample.keys())}")
            logger.warning(f"Sample: {str(sample)[:500]}")
            return self._parse_format_slots(data, target_local_date)

    def _parse_format_slots(self, data: list, target_local_date: str) -> list:
        """Parse Format A: each resource has a 'slots' array.
        Merges slots for the same resource_id (e.g. when query spans 2 UTC days).
        """
        by_resource: dict[str, dict] = {}  # resource_id → {name, slots}

        for i, resource in enumerate(data):
            resource_id = resource.get("resource_id", "")
            resource_name = (
                self._resource_names.get(resource_id)
                or resource.get("resource_name")
                or resource.get("name")
                or f"Cancha {i + 1}"
            )
            utc_date = resource.get("start_date", "")

            for slot in resource.get("slots", []):
                parsed = self._parse_slot(slot, resource_id, utc_date, target_local_date)
                if parsed:
                    if resource_id not in by_resource:
                        by_resource[resource_id] = {
                            "resource_id": resource_id,
                            "name": resource_name,
                            "slots": [],
                        }
                    by_resource[resource_id]["slots"].append(parsed)

        return list(by_resource.values())

    def _parse_format_flat(self, data: list, target_local_date: str) -> list:
        """Parse Format B: flat list where each item is one slot."""
        by_resource = {}
        for item in data:
            resource_id = item.get("resource_id", "")
            resource_name = (
                self._resource_names.get(resource_id)
                or item.get("resource_name")
                or item.get("name")
                or ""
            )
            utc_date = item.get("start_date", item.get("date", ""))

            parsed = self._parse_slot(item, resource_id, utc_date, target_local_date)
            if parsed:
                if resource_id not in by_resource:
                    by_resource[resource_id] = {
                        "resource_id": resource_id,
                        "name": resource_name or f"Cancha {len(by_resource) + 1}",
                        "slots": [],
                    }
                by_resource[resource_id]["slots"].append(parsed)

        return list(by_resource.values())

    def _parse_slot(self, slot: dict, resource_id: str, utc_date: str, target_local_date: str) -> dict | None:
        """Parse a single slot. Converts UTC→local. Filters by target local date."""
        raw_time = slot.get("start_time") or slot.get("start", "")
        if not raw_time:
            return None

        # If raw_time is a full ISO datetime, split date and time
        if "T" in raw_time:
            parts = raw_time.split("T")
            utc_date = parts[0]
            raw_time = parts[1]

        # Ensure we have a UTC date
        if not utc_date:
            return None

        # Duration → int
        try:
            duration = int(slot.get("duration", 90))
        except (ValueError, TypeError):
            duration = 90

        # Price
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

        # UTC time for display
        utc_time_short = raw_time[:5] if len(raw_time) >= 5 else raw_time
        utc_time_full = raw_time[:8] if len(raw_time) >= 8 else raw_time

        # Convert UTC → local
        local_time = self._local_time_str(utc_time_short)
        local_date = self._local_date_for_utc(utc_date, utc_time_short)

        # Filter: only keep slots that fall on the requested LOCAL date
        if local_date != target_local_date:
            return None

        # Full ISO datetime in UTC — used for Playtomic booking API
        start_iso_utc = f"{utc_date}T{utc_time_full}"

        return {
            "start": start_iso_utc,       # UTC for Playtomic booking API
            "time": local_time,            # LOCAL for display to user
            "duration": duration,
            "price": price,
            "resource_id": resource_id,
        }

    def format_availability_whatsapp(self, availability: list, date_str: str) -> str:
        """Format availability data as a WhatsApp-friendly message."""
        if not availability:
            return f"No hay canchas disponibles para el {date_str}."

        lines = [f"🎾 *Canchas disponibles — {date_str}*\n"]

        for court in availability:
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
        start_time should be in UTC ISO format (as returned by availability).
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
            # Format phone: strip leading country code formatting if needed
            phone = customer_phone.lstrip("+").strip()
            player["phone"] = phone

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
            logger.info(f"Creating booking: {resource_id} at {start_time} for '{customer_name}' phone={customer_phone}")
            r = await self.client.post(
                f"{PLAYTOMIC_API}/v1/matches",
                headers=headers,
                json=match_payload,
            )

            if r.status_code == 401:
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

            logger.info(f"Booking response: {r.status_code} {r.text[:500]}")

            if r.status_code in (200, 201):
                match_data = r.json()
                match_id = match_data.get("match_id", "")
                logger.info(f"Booking OK — match_id: {match_id}")
                return {"success": True, "booking": match_data, "match_id": match_id}
            else:
                logger.error(f"Booking failed: {r.status_code} {r.text[:300]}")
                return {"error": f"Error al crear reserva: {r.status_code} - {r.text[:200]}"}

        except Exception as e:
            logger.error(f"Booking exception: {e}")
            return {"error": f"Error de conexión: {e}"}

    # ─── LIST MATCHES (for admin/cleanup) ───
    async def list_matches(self, date_str: str) -> list:
        """List all matches/bookings for a date. Requires tenant auth."""
        await self.ensure_tenant_auth()
        if not self.tenant_token:
            return []

        # Query UTC range for full local day
        offset_h = abs(CLUB_UTC_OFFSET)
        target_date = date.fromisoformat(date_str)
        next_day = (target_date + timedelta(days=1)).isoformat()

        try:
            r = await self.client.get(
                f"{PLAYTOMIC_API}/v1/matches",
                headers={"Authorization": f"Bearer {self.tenant_token}"},
                params={
                    "tenant_id": TENANT_ID,
                    "sport_id": "PADEL",
                    "start_date_min": f"{date_str}T{offset_h:02d}:00:00",
                    "start_date_max": f"{next_day}T{offset_h:02d}:00:00",
                },
            )
            logger.info(f"List matches response: {r.status_code} {r.text[:1000]}")
            if r.status_code == 200:
                matches = r.json()
                if isinstance(matches, list):
                    return matches
                elif isinstance(matches, dict):
                    return matches.get("matches", matches.get("results", [matches]))
            return []
        except Exception as e:
            logger.error(f"List matches error: {e}")
            return []

    async def cancel_match(self, match_id: str) -> dict:
        """Cancel/delete a match by ID. Requires tenant auth."""
        await self.ensure_tenant_auth()
        if not self.tenant_token:
            return {"error": "No tenant token"}

        headers = {"Authorization": f"Bearer {self.tenant_token}"}

        try:
            # Try DELETE first
            r = await self.client.delete(
                f"{PLAYTOMIC_API}/v1/matches/{match_id}",
                headers=headers,
            )
            logger.info(f"Cancel match {match_id}: {r.status_code} {r.text[:300]}")

            if r.status_code in (200, 204):
                return {"success": True, "match_id": match_id}

            # If DELETE doesn't work, try PATCH with cancellation
            r2 = await self.client.patch(
                f"{PLAYTOMIC_API}/v1/matches/{match_id}",
                headers={**headers, "Content-Type": "application/json"},
                json={"status": "CANCELLED"},
            )
            logger.info(f"Cancel (PATCH) match {match_id}: {r2.status_code} {r2.text[:300]}")

            if r2.status_code in (200, 204):
                return {"success": True, "match_id": match_id}

            return {"error": f"Cancel failed: DELETE={r.status_code}, PATCH={r2.status_code}"}
        except Exception as e:
            logger.error(f"Cancel match error: {e}")
            return {"error": str(e)}

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
