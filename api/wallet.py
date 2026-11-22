"""
MatchBot — Monedero del club (saldo precargado).

Los clientes compran paquetes (ej. pagan $1,000 y reciben $1,300 de saldo).
El staff registra la recarga desde /panel (sección Pagos) y el bot:
  - avisa al cliente por WhatsApp que su saldo se abonó
  - al reservar (chat o llamada), si el saldo alcanza, la reserva queda
    PAGADA automáticamente con el monedero y se anota en Playtomic
  - comando *saldo* para que el cliente consulte su monedero

Todo se guarda como transacciones (wallet_tx); el saldo es la suma.
Clave canónica de cliente: últimos 10 dígitos del teléfono (igual que customers).
"""
import logging
import os

from db.database import execute

logger = logging.getLogger("matchbot.wallet")


def _phone10(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def packages() -> list[dict]:
    """Paquetes de recarga desde env WALLET_PACKAGES='1000:1300,2000:2300'."""
    raw = os.getenv("WALLET_PACKAGES", "1000:1300,2000:2300")
    out = []
    for part in raw.split(","):
        try:
            paga, recibe = part.strip().split(":")
            out.append({"paga": float(paga), "recibe": float(recibe)})
        except ValueError:
            continue
    return out


def ensure_tables():
    execute("""
        CREATE TABLE IF NOT EXISTS wallet_tx (
            id SERIAL PRIMARY KEY,
            phone10 TEXT NOT NULL,
            amount NUMERIC(10,2) NOT NULL,
            paid NUMERIC(10,2),
            concept TEXT,
            match_id TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    execute("CREATE INDEX IF NOT EXISTS wallet_tx_phone ON wallet_tx (phone10)")


def balance(phone: str) -> float:
    key = _phone10(phone)
    if not key:
        return 0.0
    row = execute("SELECT COALESCE(SUM(amount),0) AS s FROM wallet_tx WHERE phone10=%s",
                  [key], fetch_one=True)
    return float(row["s"]) if row else 0.0


def credit(phone: str, credited: float, paid: float | None = None,
           concept: str = "Recarga", created_by: str = "panel") -> float:
    """Abona saldo. Devuelve el saldo nuevo."""
    key = _phone10(phone)
    execute("""
        INSERT INTO wallet_tx (phone10, amount, paid, concept, created_by)
        VALUES (%s, %s, %s, %s, %s)
    """, [key, credited, paid, concept, created_by])
    return balance(key)


def debit(phone: str, amount: float, match_id: str = "",
          concept: str = "Pago de reserva") -> float:
    """Descuenta saldo (amount positivo). Devuelve el saldo nuevo."""
    key = _phone10(phone)
    execute("""
        INSERT INTO wallet_tx (phone10, amount, concept, match_id, created_by)
        VALUES (%s, %s, %s, %s, 'bot')
    """, [key, -abs(amount), concept, match_id])
    return balance(key)


def try_pay(phone: str, price: float, match_id: str = "") -> dict | None:
    """Si el saldo alcanza, cobra automáticamente.
    Devuelve {'paid': price, 'remaining': saldo} o None si no alcanza."""
    if price <= 0:
        return None
    try:
        bal = balance(phone)
        if bal + 0.01 >= price:
            remaining = debit(phone, price, match_id)
            return {"paid": price, "remaining": remaining}
    except Exception as e:
        logger.error(f"wallet try_pay failed: {e}")
    return None


def saldo_message(phone: str) -> str:
    bal = balance(phone)
    packs = " · ".join(f"paga ${p['paga']:.0f} y recibe ${p['recibe']:.0f}"
                       for p in packages())
    if bal > 0:
        return (f"💰 *Tu monedero del club*\n\n"
                f"Saldo disponible: *${bal:.0f} MXN*\n\n"
                f"Tus reservas se pagan automáticamente con tu saldo. "
                f"Recarga en el club: {packs}.")
    return (f"💰 *Tu monedero del club*\n\n"
            f"No tienes saldo por ahora.\n\n"
            f"Recarga en el club y tus reservas se pagan solas: {packs}.")


def all_balances(limit: int = 200) -> list[dict]:
    rows = execute("""
        SELECT phone10, COALESCE(SUM(amount),0) AS saldo,
               MAX(created_at) AS ultima
        FROM wallet_tx GROUP BY phone10
        HAVING COALESCE(SUM(amount),0) <> 0 OR MAX(created_at) > NOW() - INTERVAL '90 days'
        ORDER BY saldo DESC LIMIT %s
    """, [limit], fetch_all=True) or []
    return [dict(r) for r in rows]


def recent_tx(limit: int = 30) -> list[dict]:
    rows = execute("""
        SELECT phone10, amount, paid, concept, created_by, created_at
        FROM wallet_tx ORDER BY created_at DESC LIMIT %s
    """, [limit], fetch_all=True) or []
    return [dict(r) for r in rows]
