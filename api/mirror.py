"""
MatchBot — Espejo local de reservas (vida en ambos sistemas).

Guarda en la base de datos de MatchBot una copia completa de TODAS las
reservas: las que crea el bot/voz/panel (al instante) y las que el staff
hace en Playtomic Manager (vía sincronización cada 30 min).

Objetivo: cuando el club decida dejar Playtomic, MatchBot ya tiene todo
el histórico y puede operar solo (calendario, disponibilidad, reportes).
"""
import logging
from datetime import datetime

from db.database import execute

logger = logging.getLogger("matchbot.mirror")


def ensure_table():
    execute("""
        CREATE TABLE IF NOT EXISTS bookings_mirror (
            match_id TEXT PRIMARY KEY,
            club_id INT DEFAULT 1,
            court TEXT,
            sport TEXT,
            start_utc TIMESTAMP,
            end_utc TIMESTAMP,
            duration INT,
            customer_name TEXT,
            phone10 TEXT,
            price NUMERIC(10,2),
            payment_type TEXT,
            status TEXT,
            source TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    execute("CREATE INDEX IF NOT EXISTS bookings_mirror_start ON bookings_mirror (start_utc)")


def _parse_dt(s: str):
    try:
        return datetime.fromisoformat(str(s).rstrip("Z")[:19])
    except (ValueError, TypeError):
        return None


def record(match_id: str, court: str, sport: str, start_utc: str,
           duration: int, name: str, phone: str, price: float,
           payment_type: str, source: str, notes: str = ""):
    """Alta inmediata al crear una reserva desde bot/voz/panel."""
    if not match_id:
        return
    start = _parse_dt(start_utc)
    p10 = "".join(c for c in (phone or "") if c.isdigit())[-10:]
    try:
        execute("""
            INSERT INTO bookings_mirror
                (match_id, court, sport, start_utc, duration, customer_name,
                 phone10, price, payment_type, status, source, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'CONFIRMED',%s,%s)
            ON CONFLICT (match_id) DO UPDATE SET
                court=EXCLUDED.court, start_utc=EXCLUDED.start_utc,
                duration=EXCLUDED.duration, customer_name=EXCLUDED.customer_name,
                phone10=EXCLUDED.phone10, price=EXCLUDED.price,
                status='CONFIRMED', notes=EXCLUDED.notes, updated_at=NOW()
        """, [match_id, court, sport, start, duration, (name or "")[:120],
              p10, price, payment_type, source, (notes or "")[:500]])
    except Exception as e:
        logger.error(f"mirror record failed: {e}")


def mark_cancelled(match_id: str):
    try:
        execute("UPDATE bookings_mirror SET status='CANCELED', updated_at=NOW() "
                "WHERE match_id=%s", [match_id])
    except Exception as e:
        logger.error(f"mirror mark_cancelled failed: {e}")


def _price_from_match(m: dict) -> float:
    total = 0.0
    try:
        for reg in (m.get("registration_info") or {}).get("registrations", []):
            d = "".join(ch for ch in str(reg.get("price") or "")
                        if ch.isdigit() or ch == ".")
            if d:
                total += float(d)
    except Exception:
        pass
    return total


def upsert_from_match(m: dict):
    """Copia/actualiza una reserva tal como viene de Playtomic."""
    mid = str(m.get("match_id") or "")
    if not mid:
        return
    start = _parse_dt(m.get("start_date"))
    end = _parse_dt(m.get("end_date"))
    dur = int((end - start).total_seconds() // 60) if (start and end) else None
    name, phone = "", ""
    for t in m.get("teams", []):
        for p in t.get("players", []):
            if p.get("name") and not name:
                name = p["name"]
    if "(" in name and name.rstrip().endswith(")"):
        base, tel = name.rsplit("(", 1)
        name = base.strip()
        phone = "".join(c for c in tel if c.isdigit())
    status = str(m.get("status") or "")
    pay = str((m.get("registration_info") or {}).get("payment_type") or "")
    try:
        execute("""
            INSERT INTO bookings_mirror
                (match_id, court, sport, start_utc, end_utc, duration,
                 customer_name, phone10, price, payment_type, status,
                 source, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'playtomic-sync',%s)
            ON CONFLICT (match_id) DO UPDATE SET
                court=EXCLUDED.court, start_utc=EXCLUDED.start_utc,
                end_utc=EXCLUDED.end_utc, duration=EXCLUDED.duration,
                customer_name=EXCLUDED.customer_name,
                phone10=CASE WHEN EXCLUDED.phone10 <> '' THEN EXCLUDED.phone10
                             ELSE bookings_mirror.phone10 END,
                price=CASE WHEN EXCLUDED.price > 0 THEN EXCLUDED.price
                           ELSE bookings_mirror.price END,
                payment_type=EXCLUDED.payment_type,
                status=EXCLUDED.status, updated_at=NOW()
        """, [mid, m.get("resource_name") or "", str(m.get("sport_id") or ""),
              start, end, dur, (name or "")[:120], phone[-10:],
              _price_from_match(m), pay, status,
              (m.get("private_notes") or "")[:500]])
    except Exception as e:
        logger.error(f"mirror upsert failed: {e}")


async def sync(playtomic) -> dict:
    """Copia todo lo visible en Playtomic al espejo local."""
    try:
        matches = await playtomic.list_matches()
    except Exception as e:
        logger.error(f"mirror sync list failed: {e}")
        return {"ok": False, "error": str(e)[:200]}
    n = 0
    for m in matches:
        upsert_from_match(m)
        n += 1
    row = execute("SELECT COUNT(*) AS n FROM bookings_mirror", fetch_one=True)
    total = int(row["n"]) if row else 0
    logger.info(f"🪞 Mirror sync: {n} procesadas, {total} en espejo")
    return {"ok": True, "synced": n, "total_local": total}
