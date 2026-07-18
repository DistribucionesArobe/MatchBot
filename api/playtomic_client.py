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
import uuid
import time
import base64
import json as json_module
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Config ───
PLAYTOMIC_API = "https://manager.playtomic.io/api"  # Manager proxy (api.playtomic.io blocked by CloudFront)
MANAGER_API = "https://manager.playtomic.io/api"   # Same base — all calls go through Manager proxy
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
        self.client = httpx.AsyncClient(
            timeout=15.0,
            http2=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
                "Origin": "https://playtomic.io",
                "Referer": "https://playtomic.io/",
                "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
            },
        )
        self._resource_names: dict[str, str] = {}  # resource_id → display name cache
        self._resource_sports: dict[str, str] = {}  # resource_id → sport_id cache

    # ─── AUTH ───
    async def login(self) -> bool:
        """Step 1: Authenticate with email/password → customer token + refresh token.
        Uses the Manager proxy auth endpoint so the token includes
        role_tenant_manager / role_activity_manager claims needed for
        Manager proxy booking creation (price calculation, etc.).
        Falls back to public API if Manager proxy login fails.
        """
        if not PLAYTOMIC_EMAIL or not PLAYTOMIC_PASSWORD:
            logger.error("PLAYTOMIC_EMAIL / PLAYTOMIC_PASSWORD not set")
            return False

        # Try Manager proxy first (grants Manager role claims in token)
        for base, label in [(MANAGER_API, "Manager proxy"), (PLAYTOMIC_API, "public API")]:
            try:
                logger.info(f"Attempting login via {label} ({base}/v3/auth/login)")
                r = await self.client.post(
                    f"{base}/v3/auth/login",
                    json={
                        "email": PLAYTOMIC_EMAIL,
                        "password": PLAYTOMIC_PASSWORD,
                        "audience": "com.playtomic.manager",
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    self.token = data.get("access_token")
                    self.refresh_token = data.get("refresh_token")
                    self.user_id = data.get("user_id")
                    # Set Authorization header on httpx client so ALL
                    # subsequent requests (availability, tenant info,
                    # resource names, etc.) carry the Bearer token.
                    self.client.headers["Authorization"] = f"Bearer {self.token}"
                    logger.info(f"Playtomic login OK via {label} — user_id: {self.user_id}")
                    # Debug: decode refresh token to check audience/claims
                    try:
                        rt_parts = self.refresh_token.split(".")
                        rt_payload = json_module.loads(base64.b64decode(rt_parts[1] + "=="))
                        logger.info(f"Login refresh_token claims: aud={rt_payload.get('aud')}, scopes={rt_payload.get('scopes')}")
                    except Exception as e:
                        logger.warning(f"Could not decode refresh_token: {e}")
                    return True
                else:
                    logger.warning(f"Login via {label} failed: {r.status_code} {r.text[:300]}")
            except Exception as e:
                logger.warning(f"Login via {label} error: {e}")

        logger.error("Playtomic login failed via all endpoints")
        return False

    async def get_tenant_token(self) -> bool:
        """Step 2: Exchange refresh_token for a tenant-scoped access token.
        Uses Manager proxy endpoint so the returned token includes
        role_tenant_manager / role_activity_manager claims needed for
        Manager-proxy booking creation (price calculation, payment info).
        Falls back to public API if Manager proxy fails.
        """
        if not self.refresh_token:
            logger.error("No refresh token available for tenant token exchange")
            return False

        # Try Manager proxy first, then public API.
        # Manager proxy adds role claims; public API does not.
        endpoints = [
            (MANAGER_API, "Manager proxy"),
            (PLAYTOMIC_API, "public API"),
        ]

        for base, label in endpoints:
            for audience in ["com.playtomic.manager", None]:
                try:
                    payload = {
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                        "scope": f"tenant:{TENANT_ID}",
                    }
                    if audience:
                        payload["audience"] = audience

                    aud_label = f"audience={audience}" if audience else "no audience"
                    logger.info(f"Requesting tenant token via {label} ({aud_label})")
                    r = await self.client.post(
                        f"{base}/v3/auth/token",
                        json=payload,
                    )

                    if r.status_code == 200:
                        data = r.json()
                        self.tenant_token = data.get("access_token")
                        new_rt = data.get("refresh_token")
                        if new_rt:
                            self.refresh_token = new_rt
                        # Update default Authorization header with tenant token
                        # (stronger than customer token — has role claims)
                        self.client.headers["Authorization"] = f"Bearer {self.tenant_token}"
                        # Debug: decode tenant token to check role claims
                        try:
                            tt_parts = self.tenant_token.split(".")
                            tt_payload = json_module.loads(base64.b64decode(tt_parts[1] + "=="))
                            logger.info(
                                f"Tenant token claims: aud={tt_payload.get('aud')}, "
                                f"scopes={tt_payload.get('scopes')}, "
                                f"has_role_tenant_manager={bool(tt_payload.get('role_tenant_manager'))}, "
                                f"all_keys={list(tt_payload.keys())}"
                            )
                        except Exception as e:
                            logger.warning(f"Could not decode tenant_token: {e}")
                        logger.info(f"Tenant token obtained OK via {label} ({aud_label})")
                        return True
                    else:
                        logger.warning(
                            f"Tenant token via {label} ({aud_label}) failed: "
                            f"{r.status_code} {r.text[:300]}"
                        )
                except Exception as e:
                    logger.warning(f"Tenant token via {label} ({aud_label}) error: {e}")

        logger.error("Could not obtain tenant token via any endpoint/audience")
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

        # Ensure we're authenticated (Manager proxy requires auth)
        await self.ensure_auth()

        # Try multiple endpoints to find resource names
        # Method 1: /v1/tenants/{id} — tenant info with resources
        try:
            r = await self.client.get(f"{PLAYTOMIC_API}/v1/tenants/{TENANT_ID}")
            if r.status_code == 200:
                info = r.json()
                logger.info(f"Tenant info keys: {list(info.keys())}")

                # Try different locations where resources might be
                resources = info.get("resources", [])
                if not resources:
                    resources = info.get("facilities", [])

                for res in resources:
                    rid = res.get("resource_id", res.get("id", ""))
                    name = res.get("name", res.get("resource_name", ""))
                    sport = res.get("sport_id", "PADEL")
                    if rid and name:
                        self._resource_names[rid] = name
                        self._resource_sports[rid] = sport

                logger.info(f"Method 1: loaded {len(self._resource_names)} names: {list(self._resource_names.values())}")
        except Exception as e:
            logger.warning(f"Tenant info error: {e}")

        # Method 2: if Method 1 didn't work, try /v1/tenants/{id}/resources
        if not self._resource_names:
            try:
                r = await self.client.get(f"{PLAYTOMIC_API}/v1/tenants/{TENANT_ID}/resources")
                if r.status_code == 200:
                    resources = r.json()
                    if isinstance(resources, list):
                        for res in resources:
                            rid = res.get("resource_id", res.get("id", ""))
                            name = res.get("name", res.get("resource_name", ""))
                            sport = res.get("sport_id", "PADEL")
                            if rid and name:
                                self._resource_names[rid] = name
                                self._resource_sports[rid] = sport
                    logger.info(f"Method 2: loaded {len(self._resource_names)} names: {list(self._resource_names.values())}")
                else:
                    logger.warning(f"Resources endpoint: {r.status_code} {r.text[:200]}")
            except Exception as e:
                logger.warning(f"Resources endpoint error: {e}")

        if not self._resource_names:
            logger.warning("Could not load resource names from any endpoint")

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

    # ─── AVAILABILITY ───
    async def get_availability(self, date_str: str) -> list:
        """
        Get court availability for a given date (local).
        date_str: 'YYYY-MM-DD' in LOCAL timezone.
        Returns list of available slots grouped by court, with LOCAL times.
        """
        # Ensure we're authenticated (Manager proxy requires auth for all endpoints)
        await self.ensure_auth()
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
            # Query availability for all sports (PADEL + FOOTBALL7)
            # Each sport is wrapped in its own try/except so a failure
            # in one sport doesn't kill the other.
            all_data = []
            for sport in ["PADEL", "FOOTBALL7"]:
                try:
                    params = {
                        "user_id": "me",
                        "sport_id": sport,
                        "tenant_id": TENANT_ID,
                        "local_start_min": f"{date_str}T00:00:00",
                        "local_start_max": f"{date_str}T23:59:59",
                        "start_min": utc_start,
                        "start_max": utc_end,
                    }
                    logger.info(f"Querying {sport} availability: date={date_str}")
                    r = await self.client.get(
                        f"{PLAYTOMIC_API}/v1/availability",
                        params=params,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            logger.info(f"{sport} availability: {len(data)} items")
                            all_data.extend(data)
                    else:
                        logger.warning(f"{sport} availability error: {r.status_code}")
                except Exception as e:
                    logger.warning(f"{sport} availability query failed: {e}")

            data = all_data
            logger.info(f"Total availability: {len(data)} items (all sports)")
            availability = self._parse_availability(data, date_str)

            # Cross-reference with existing bookings to remove
            # slots that are already reserved (the availability API
            # sometimes still returns booked slots).
            try:
                existing = await self.list_matches(date_str)
                if existing:
                    availability = self._filter_booked_slots(
                        availability, existing
                    )
            except Exception as e:
                logger.warning(f"Could not filter booked slots: {e}")

            return availability
        except Exception as e:
            logger.error(f"Availability error: {e}")
            return []

    def _filter_booked_slots(self, availability: list, bookings: list) -> list:
        """Remove slots that overlap with existing bookings.
        Each booking has resource_id, start_date, end_date (UTC ISO).
        Each slot has resource_id, start (UTC ISO), duration (minutes).
        """
        # Build a list of (resource_id, start_dt, end_dt) from bookings
        booked_ranges = []
        for b in bookings:
            rid = b.get("resource_id", "")
            sd = b.get("start_date", b.get("start", ""))
            ed = b.get("end_date", b.get("end", ""))
            status = b.get("status", "")
            # Skip cancelled bookings
            if status in ("CANCELLED", "REJECTED"):
                continue
            if rid and sd and ed:
                try:
                    s = datetime.fromisoformat(sd.replace("Z", ""))
                    e = datetime.fromisoformat(ed.replace("Z", ""))
                    booked_ranges.append((rid, s, e))
                except (ValueError, TypeError):
                    pass

        if not booked_ranges:
            return availability

        logger.info(f"Filtering availability against {len(booked_ranges)} existing bookings")

        filtered = []
        for court in availability:
            court_rid = court.get("resource_id", "")
            kept_slots = []
            for slot in court.get("slots", []):
                slot_start_str = slot.get("start", "")
                slot_dur = slot.get("duration", 90)
                try:
                    slot_start = datetime.fromisoformat(slot_start_str.replace("Z", ""))
                    slot_end = slot_start + timedelta(minutes=slot_dur)
                except (ValueError, TypeError):
                    kept_slots.append(slot)
                    continue

                # Check overlap with any booking on the same court
                is_booked = False
                for b_rid, b_start, b_end in booked_ranges:
                    if b_rid == court_rid:
                        # Overlap: slot_start < booking_end AND slot_end > booking_start
                        if slot_start < b_end and slot_end > b_start:
                            is_booked = True
                            break

                if not is_booked:
                    kept_slots.append(slot)

            if kept_slots:
                filtered.append({**court, "slots": kept_slots})

        removed = sum(len(c["slots"]) for c in availability) - sum(len(c["slots"]) for c in filtered)
        if removed:
            logger.info(f"Filtered out {removed} already-booked slots")
        return filtered

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

        # Separate padel and football courts
        padel_courts = []
        football_courts = []
        for court in availability:
            rid = court.get("resource_id", "")
            sport = self._resource_sports.get(rid, "PADEL")
            if sport == "FOOTBALL7":
                football_courts.append(court)
            else:
                padel_courts.append(court)

        lines = []
        if padel_courts:
            lines.append(f"🎾 *Canchas de Padel — {date_str}*\n")
            for court in padel_courts:
                lines.append(f"*{court['name']}*")
                slot_texts = []
                for slot in court["slots"]:
                    price_str = f"${slot['price']}" if slot['price'] else ""
                    slot_texts.append(f"  {slot['time']} ({slot['duration']}min) {price_str}")
                lines.append("\n".join(slot_texts))
                lines.append("")

        if football_courts:
            lines.append(f"⚽ *Canchas de Fútbol — {date_str}*\n")
            for court in football_courts:
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
        slot_price: float = 0.0,
    ) -> dict:
        """
        Create a booking on Playtomic as the club manager.
        Two-step process:
          1. POST /v1/matches — creates the match/booking
          2. POST /v1/matches/{id}/players — adds the player name
        The first call ignores teams/players data, so we add the player
        separately via the dedicated addPlayer endpoint.
        start_time should be in UTC ISO format (as returned by availability).
        """
        await self.ensure_tenant_auth()
        if not self.tenant_token:
            return {"error": "No se pudo obtener token de manager Playtomic"}

        headers = {
            "Authorization": f"Bearer {self.tenant_token}",
            "Content-Type": "application/json",
            # Manager proxy requires these headers (intercepted from Manager UI)
            "x-requested-with": "com.playtomic.manager",
            "x-authorization-scope": f"tenant:{TENANT_ID}",
        }

        # Calculate end_time from start + duration
        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = start_dt + timedelta(minutes=duration)
            end_time = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            end_time = start_time  # fallback

        MANAGER_API = "https://manager.playtomic.io/api"

        # Detect sport from resource_id
        sport_id = self._resource_sports.get(resource_id, "PADEL")
        is_football = sport_id == "FOOTBALL7"
        # Padel: 2 per team × 2 = 4 players; Football 7: 7 per team × 2 = 14
        max_per_team = 7 if is_football else 2
        num_players = max_per_team * 2

        # Build player info for registration
        player_merchant_id = None
        player_display_name = customer_name or "WhatsApp"

        # Search for existing customer first
        existing = None
        if customer_phone:
            existing = await self._search_customer_by_phone(headers, customer_phone)

        if existing and existing.get("user_id"):
            player_merchant_id = existing["user_id"]
            player_display_name = existing.get("full_name") or customer_name or "WhatsApp"
            player_type = "CUSTOMER"
        else:
            timestamp_ms = int(time.time() * 1000)
            random_hex = uuid.uuid4().hex[:8]
            player_merchant_id = f"guest:{timestamp_ms}:{random_hex}"
            if customer_phone:
                phone_clean = customer_phone.lstrip("+").strip()
                player_display_name = f"{player_display_name} ({phone_clean[-10:]})"
            player_type = "GUEST"

        # ── Attempt 1: Create via Manager proxy with full payload ──
        # The Manager proxy uses snake_case (NOT camelCase). Including
        # registration_info triggers automatic price calculation on the
        # server, producing a booking identical to ones created from the
        # Manager UI — with the Payment section visible.
        # Format dates with Z suffix like the Manager UI does
        start_date_z = start_time.rstrip("Z") + "Z" if not start_time.endswith("Z") else start_time
        end_date_z = end_time.rstrip("Z") + "Z" if not end_time.endswith("Z") else end_time

        # Build per-person price for split payment
        # Padel: 300/4 = 75 MXN per person; Football: price/14 per person
        per_person_price = f"{int(slot_price / num_players)} MXN" if slot_price > 0 else None

        # Build registration entries — all 4 get a price for split payment
        first_registration = {
            "merchant_player_id": player_merchant_id,
            "name": player_display_name,
        }
        empty_registration = {}

        if per_person_price:
            first_registration["price"] = per_person_price
            first_registration["paid"] = False
            empty_registration = {"price": per_person_price, "paid": False}

        # Build registrations list: first player + (num_players - 1) empty slots
        registrations = [first_registration] + [dict(empty_registration) for _ in range(num_players - 1)]

        manager_payload = {
            "sport_id": sport_id,
            "tenant_id": TENANT_ID,
            "resource_id": resource_id,
            "start_date": start_date_z,
            "end_date": end_date_z,
            "visibility": "HIDDEN",
            "is_playtomic_managed": False,
            "max_players_per_team": max_per_team,
            "description": None,
            "private_notes": None,
            "registration_info": {
                "payment_type": "SPLIT",
                "registrations": registrations,
            },
        }

        try:
            logger.info(f"Creating {sport_id} booking via Manager proxy: {resource_id} at {start_time}")
            r = await self.client.post(
                f"{MANAGER_API}/v1/matches",
                headers=headers,
                json=manager_payload,
            )
            logger.info(f"Manager proxy create: {r.status_code} {r.text[:500]}")

            if r.status_code in (200, 201):
                match_data = r.json()
                match_id = match_data.get("match_id", match_data.get("matchId", ""))
                logger.info(f"Booking OK via Manager — match_id: {match_id}")
                return {"success": True, "booking": match_data, "match_id": match_id}
            else:
                logger.warning(f"Manager proxy create failed ({r.status_code}), falling back to public API")
        except Exception as e:
            logger.warning(f"Manager proxy create exception: {e}, falling back to public API")

        # ── Attempt 2: Create via public API (fallback) ──
        # Include registration_info with price so the Manager UI shows the
        # Payment section. The price is sent by the client, NOT calculated
        # server-side.
        match_payload = {
            "sport_id": sport_id,
            "tenant_id": TENANT_ID,
            "resource_id": resource_id,
            "start_date": start_date_z,
            "end_date": end_date_z,
            "match_type": "BOOKING",
            "match_organization": "TENANT",
            "visibility": "HIDDEN",
            "match_origin": "PLAYTOMIC_MANAGER",
            "competition_mode": "COMPETITIVE",
            "min_players_per_team": max_per_team,
            "max_players_per_team": max_per_team,
            "owner_id": self.user_id,
            "registration_info": {
                "payment_type": "SPLIT",
                "registrations": registrations,
            },
        }

        try:
            logger.info(f"Creating {sport_id} booking via public API: {resource_id} at {start_time} for '{customer_name}'")
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

            logger.info(f"Public API create: {r.status_code} {r.text[:500]}")

            if r.status_code in (200, 201):
                match_data = r.json()
                match_id = match_data.get("match_id", "")
                logger.info(f"Booking OK via public API — match_id: {match_id}")

                # Add player to the booking
                if match_id and (customer_name or customer_phone):
                    await self._add_player_to_match(
                        match_id, headers, customer_name, customer_phone
                    )

                # registration_info with price is now included in the POST
                # payload, so no separate PATCH is needed.

                return {"success": True, "booking": match_data, "match_id": match_id}
            else:
                logger.error(f"Booking failed: {r.status_code} {r.text[:300]}")
                return {"error": f"Error al crear reserva: {r.status_code} - {r.text[:200]}"}

        except Exception as e:
            logger.error(f"Booking exception: {e}")
            return {"error": f"Error de conexión: {e}"}

    async def _add_registration_info(
        self, match_id: str, headers: dict,
        player_merchant_id: str = "", player_name: str = ""
    ) -> None:
        """
        Try to add registrationInfo to a match so the Manager shows the
        Payment section with prices. Tries Manager proxy PATCH with
        camelCase payload (the format the Manager UI uses).
        """
        MANAGER_API = "https://manager.playtomic.io/api"

        # camelCase payload — matches what the Manager JS sends
        reg_payload = {
            "registrationInfo": {
                "paymentType": "SINGLE_PAYER",
                "registrations": [
                    {
                        "merchantPlayerId": player_merchant_id,
                        "name": player_name,
                    },
                    {"name": None},
                    {"name": None},
                    {"name": None},
                ],
            },
        }

        # Try Manager proxy PATCH (camelCase)
        try:
            logger.info(f"PATCH registrationInfo on {match_id} via Manager proxy")
            r = await self.client.patch(
                f"{MANAGER_API}/v1/matches/{match_id}",
                headers=headers,
                json=reg_payload,
            )
            logger.info(f"registrationInfo PATCH (Manager): {r.status_code} {r.text[:300]}")
            if r.status_code in (200, 204):
                logger.info(f"registrationInfo added OK via Manager proxy")
                return
        except Exception as e:
            logger.warning(f"registrationInfo PATCH (Manager) exception: {e}")

        # Try public API PATCH (snake_case)
        try:
            logger.info(f"PATCH registration_info on {match_id} via public API")
            r = await self.client.patch(
                f"{PLAYTOMIC_API}/v1/matches/{match_id}",
                headers=headers,
                json={
                    "registration_info": {
                        "payment_type": "SINGLE_PAYER",
                        "registrations": [
                            {
                                "merchant_player_id": player_merchant_id,
                                "name": player_name,
                            },
                        ],
                    },
                },
            )
            logger.info(f"registration_info PATCH (Public): {r.status_code} {r.text[:300]}")
            if r.status_code in (200, 204):
                logger.info(f"registration_info added OK via public API")
                return
        except Exception as e:
            logger.warning(f"registration_info PATCH (Public) exception: {e}")

        logger.warning(f"Could not add registrationInfo to match {match_id}")

    async def _search_customer_by_phone(
        self, headers: dict, phone: str
    ) -> dict | None:
        """
        Search for an existing customer in Playtomic by phone number.
        Uses GET /api/v2/users/suggestions/players (Manager proxy).
        This is the same endpoint the Manager UI uses in the Owner/Player
        search fields — it searches across name, phone, and email.
        Returns {user_id, full_name, phone, email} if found, else None.
        """
        MANAGER_API = "https://manager.playtomic.io/api"

        # Normalize phone: strip + and spaces, try different formats
        phone_clean = phone.lstrip("+").replace(" ", "").strip()

        # Try: last 10 digits first (more likely to match), then full number
        search_variants = []
        if len(phone_clean) > 10:
            search_variants.append(phone_clean[-10:])  # without country code
        search_variants.append(phone_clean)

        for search_term in search_variants:
            try:
                logger.info(f"Searching player by phone: '{search_term}'")
                r = await self.client.get(
                    f"{MANAGER_API}/v2/users/suggestions/players",
                    headers=headers,
                    params={
                        "tenant_id": TENANT_ID,
                        "filter": search_term,
                        "size": "5",
                    },
                )
                logger.info(f"Player search status={r.status_code} body={r.text[:300]}")
                if r.status_code == 200:
                    results = r.json()
                    if isinstance(results, list) and len(results) > 0:
                        # Use the first result — the API already does fuzzy matching
                        player = results[0]
                        result = {
                            "user_id": str(player.get("user_id", "")),
                            "full_name": player.get("full_name", ""),
                            "phone": player.get("phone", ""),
                            "email": player.get("email", ""),
                        }
                        logger.info(
                            f"Player found: '{result['full_name']}' "
                            f"user_id={result['user_id']}"
                        )
                        return result
                    else:
                        logger.info(f"No players found for '{search_term}'")
                else:
                    logger.warning(f"Player search returned {r.status_code}")
            except Exception as e:
                logger.warning(f"Player search exception: {e}")

        return None

    async def _add_player_to_match(
        self,
        match_id: str,
        headers: dict,
        customer_name: str = "",
        customer_phone: str = "",
    ) -> None:
        """
        Add a player to an existing match via POST /v1/matches/{id}/players.
        This makes the player name appear in Playtomic Manager's booking list.

        Flow:
          1. Search for existing customer by phone in Playtomic DB
          2. If found → use their user_id and full_name (links to real profile)
          3. If not found → create as guest with WhatsApp name + phone

        IMPORTANT: Uses the Manager API proxy (manager.playtomic.io/api) instead
        of the public API (api.playtomic.io) because the public API doesn't
        properly persist player data on matches.
        """
        MANAGER_API = "https://manager.playtomic.io/api"

        # Step 1: Try to find existing customer by phone
        existing_customer = None
        if customer_phone:
            existing_customer = await self._search_customer_by_phone(
                headers, customer_phone
            )

        if existing_customer and existing_customer.get("user_id"):
            # Use the real customer profile
            display_name = existing_customer["full_name"] or customer_name or "WhatsApp"
            player_id = existing_customer["user_id"]
            logger.info(
                f"Linking booking to existing customer: '{display_name}' "
                f"(user_id={player_id[:8]}...)"
            )
        else:
            # Fallback: create as guest with WhatsApp name + phone
            timestamp_ms = int(time.time() * 1000)
            random_hex = uuid.uuid4().hex[:8]
            player_id = f"guest:{timestamp_ms}:{random_hex}"
            display_name = customer_name or "WhatsApp"
            if customer_phone:
                phone_clean = customer_phone.lstrip("+").strip()
                display_name = f"{display_name} ({phone_clean[-10:]})"
            logger.info(f"No existing customer found, using guest: '{display_name}'")

        player_payload = {
            "name": display_name,
            "merchant_player_id": player_id,
            "team_id": "0",
        }

        # Try Manager API first (what the UI uses), fall back to public API
        endpoints = [
            (f"{MANAGER_API}/v1/matches/{match_id}/players", "Manager API"),
            (f"{PLAYTOMIC_API}/v1/matches/{match_id}/players", "Public API"),
        ]

        for url, label in endpoints:
            try:
                logger.info(f"Adding player '{display_name}' to match {match_id} via {label}")
                r = await self.client.post(url, headers=headers, json=player_payload)
                logger.info(f"Add player via {label}: status={r.status_code} body={r.text[:300]}")
                if r.status_code == 200:
                    logger.info(f"Player added OK to match {match_id} via {label}")
                    return  # Success
                else:
                    logger.warning(f"Add player via {label} returned {r.status_code}, trying next...")
            except Exception as e:
                logger.warning(f"Add player via {label} exception: {e}, trying next...")

        logger.warning(f"Could not add player to match {match_id} via any endpoint")

    # ─── LIST MATCHES (for admin/cleanup) ───
    async def list_matches(self, date_str: str = None) -> list:
        """List matches/bookings. If date_str given, filter to that local date.
        Queries ALL matches from Playtomic and filters client-side since
        the API ignores start_date_min/max parameters.
        """
        await self.ensure_tenant_auth()
        if not self.tenant_token:
            return []

        all_matches = []

        try:
            # Query without date filter — API ignores it anyway
            # Try multiple pages/sort orders to get comprehensive results
            for sort_param in ["start_date:asc", "start_date:desc"]:
                r = await self.client.get(
                    f"{PLAYTOMIC_API}/v1/matches",
                    headers={"Authorization": f"Bearer {self.tenant_token}"},
                    params={
                        "tenant_id": TENANT_ID,
                        "sport_id": "PADEL",
                        "sort": sort_param,
                        "size": 200,
                    },
                )
                logger.info(f"List matches ({sort_param}): {r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        all_matches.extend(data)
                    elif isinstance(data, dict):
                        items = data.get("matches", data.get("results", []))
                        all_matches.extend(items)

            # Deduplicate by match_id
            seen = set()
            unique = []
            for m in all_matches:
                mid = m.get("match_id", "")
                if mid and mid not in seen:
                    seen.add(mid)
                    unique.append(m)

            # Client-side date filter if requested
            if date_str:
                offset_h = abs(CLUB_UTC_OFFSET)
                target = date.fromisoformat(date_str)
                next_day = target + timedelta(days=1)

                # UTC range for full local day
                utc_start = datetime(target.year, target.month, target.day, offset_h, 0, 0)
                utc_end = datetime(next_day.year, next_day.month, next_day.day, offset_h, 0, 0)

                filtered = []
                for m in unique:
                    sd = m.get("start_date", m.get("start", ""))
                    if sd:
                        try:
                            match_dt = datetime.fromisoformat(sd.replace("Z", ""))
                            if utc_start <= match_dt < utc_end:
                                filtered.append(m)
                        except (ValueError, TypeError):
                            pass
                logger.info(f"Date filter {date_str}: {len(filtered)} of {len(unique)} matches")
                return filtered

            return unique
        except Exception as e:
            logger.error(f"List matches error: {e}")
            return []

    async def list_all_matches_raw(self) -> list:
        """List ALL matches without any filter. For finding test bookings."""
        await self.ensure_tenant_auth()
        if not self.tenant_token:
            return []

        try:
            r = await self.client.get(
                f"{PLAYTOMIC_API}/v1/matches",
                headers={"Authorization": f"Bearer {self.tenant_token}"},
                params={
                    "tenant_id": TENANT_ID,
                    "size": 500,
                },
            )
            logger.info(f"List ALL matches: {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    return data.get("matches", data.get("results", []))
            return []
        except Exception as e:
            logger.error(f"List all matches error: {e}")
            return []

    async def search_bookings(self, date_str: str) -> dict:
        """Search for bookings using multiple Playtomic API endpoints.
        Tries /v1/matches, /v1/reservations, and tenant-scoped queries.
        """
        await self.ensure_tenant_auth()
        if not self.tenant_token:
            return {"error": "No auth"}

        headers = {"Authorization": f"Bearer {self.tenant_token}"}
        results = {}

        offset_h = abs(CLUB_UTC_OFFSET)
        target = date.fromisoformat(date_str)
        next_day = (target + timedelta(days=1)).isoformat()
        utc_start = f"{date_str}T{offset_h:02d}:00:00"
        utc_end = f"{next_day}T{offset_h:02d}:00:00"

        # Try multiple endpoints and param combinations
        queries = [
            ("matches_by_date", f"/v1/matches", {
                "tenant_id": TENANT_ID,
                "start_min": utc_start, "start_max": utc_end,
            }),
            ("matches_by_start_date", f"/v1/matches", {
                "tenant_id": TENANT_ID,
                "start_date_gte": utc_start, "start_date_lte": utc_end,
            }),
            ("reservations", f"/v1/tenants/{TENANT_ID}/reservations", {
                "start_min": utc_start, "start_max": utc_end,
            }),
            ("bookings", f"/v1/tenants/{TENANT_ID}/bookings", {
                "date": date_str,
            }),
            ("matches_tenant", f"/v1/tenants/{TENANT_ID}/matches", {
                "start_min": utc_start, "start_max": utc_end,
            }),
            ("calendar", f"/v1/tenants/{TENANT_ID}/calendar", {
                "date": date_str,
            }),
        ]

        for name, endpoint, params in queries:
            try:
                r = await self.client.get(
                    f"{PLAYTOMIC_API}{endpoint}",
                    headers=headers,
                    params=params,
                )
                status = r.status_code
                if status == 200:
                    data = r.json()
                    if isinstance(data, list):
                        results[name] = {"status": status, "count": len(data), "sample": str(data[:2])[:500]}
                    elif isinstance(data, dict):
                        results[name] = {"status": status, "keys": list(data.keys()), "sample": str(data)[:500]}
                    else:
                        results[name] = {"status": status, "type": str(type(data))}
                else:
                    results[name] = {"status": status, "error": r.text[:200]}
            except Exception as e:
                results[name] = {"error": str(e)}

        return results

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
