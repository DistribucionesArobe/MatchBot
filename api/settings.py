"""
MatchBot — Configuración del club editable desde /panel (⚙️ Configuración).

Valores en la tabla app_settings (clave/valor) con caché de 60 s.
Si una clave no existe en la BD, se usa la variable de entorno o el default.

Claves:
  h_lv_abre / h_lv_cierra     horario lunes-viernes  (HH:MM)
  h_sab_abre / h_sab_cierra   horario sábado
  h_dom_abre / h_dom_cierra   horario domingo
  precio_respaldo             precio si Playtomic no cotiza (MXN)
  precio_clase                precio de clase privada (MXN)
  paquetes_monedero           "1000:1300,2000:2300"
  tel_avisos                  WhatsApp del dueño/tienda para avisos
  tel_clases                  WhatsApp que recibe solicitudes de clase
"""
import logging
import os
import time as _time

from db.database import execute

logger = logging.getLogger("matchbot.settings")

_cache: dict = {}
_cache_at: float = 0.0
CACHE_S = 60

DEFAULTS = {
    "h_lv_abre": "06:30",
    "h_lv_cierra": "23:30",
    "h_sab_abre": "07:30",
    "h_sab_cierra": "21:00",
    "h_dom_abre": "07:30",
    "h_dom_cierra": "21:30",
    "precio_respaldo": lambda: os.getenv("FALLBACK_PRICE", "300"),
    "precio_clase": lambda: os.getenv("CLASS_PRICE", "600"),
    "paquetes_monedero": lambda: os.getenv("WALLET_PACKAGES", "1000:1300,2000:2300"),
    "tel_avisos": lambda: os.getenv("CLUB_NOTIFY_PHONE", "528342546466"),
    "tel_clases": lambda: os.getenv("CLASS_NOTIFY_PHONE", "528342546466"),
}

LABELS = {
    "h_lv_abre": "Lun-Vie abre", "h_lv_cierra": "Lun-Vie cierra",
    "h_sab_abre": "Sábado abre", "h_sab_cierra": "Sábado cierra",
    "h_dom_abre": "Domingo abre", "h_dom_cierra": "Domingo cierra",
    "precio_respaldo": "Precio de respaldo (MXN)",
    "precio_clase": "Precio de clase (MXN)",
    "paquetes_monedero": "Paquetes de monedero (paga:recibe, separados por coma)",
    "tel_avisos": "WhatsApp de avisos del club",
    "tel_clases": "WhatsApp para clases",
}


def ensure_table():
    execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)


def _load() -> dict:
    global _cache, _cache_at
    if _time.time() - _cache_at < CACHE_S and _cache:
        return _cache
    try:
        rows = execute("SELECT key, value FROM app_settings", fetch_all=True) or []
        _cache = {r["key"]: r["value"] for r in rows}
        _cache_at = _time.time()
    except Exception as e:
        logger.error(f"settings load failed: {e}")
    return _cache


def get(key: str, default: str = "") -> str:
    val = _load().get(key)
    if val not in (None, ""):
        return val
    d = DEFAULTS.get(key, default)
    return d() if callable(d) else (d or default)


def set_value(key: str, value: str):
    global _cache_at
    execute("""
        INSERT INTO app_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, [key, (value or "").strip()])
    _cache_at = 0.0  # invalida caché


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get(key))
    except (ValueError, TypeError):
        return default


def _hhmm_to_min(hhmm: str, default: int) -> int:
    try:
        h, m = hhmm.strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default


def club_hours(weekday: int) -> tuple[int, int]:
    """(minuto_abre, minuto_cierra) para el día (0=lunes … 6=domingo)."""
    if weekday <= 4:
        return (_hhmm_to_min(get("h_lv_abre"), 390),
                _hhmm_to_min(get("h_lv_cierra"), 1410))
    if weekday == 5:
        return (_hhmm_to_min(get("h_sab_abre"), 450),
                _hhmm_to_min(get("h_sab_cierra"), 1260))
    return (_hhmm_to_min(get("h_dom_abre"), 450),
            _hhmm_to_min(get("h_dom_cierra"), 1290))


def all_values() -> dict:
    return {k: get(k) for k in DEFAULTS}
