"""
MatchBot — Autenticación del panel (login + roles).

Roles:
  superadmin — ve y configura todo, administra usuarios (Alejandro)
  staff      — ve calendario/reservas/clientes/pagos y registra recargas

Primer uso: al arrancar, si no hay usuarios, se crea el superadmin
  usuario:  alejandro
  password: env SUPERADMIN_PASSWORD (o, si no está, VOICE_API_KEY)
Sesión: JWT firmado en cookie 'panel_session' (12 horas).
"""
import logging
import os
from datetime import datetime, timedelta

from db.database import execute

logger = logging.getLogger("matchbot.auth")

COOKIE = "panel_session"
SESSION_HOURS = 12


def _secret() -> str:
    return (os.getenv("PANEL_SECRET", "") or os.getenv("VOICE_API_KEY", "")
            or "matchbot-panel-secret")


def _hash(password: str) -> str:
    from passlib.hash import bcrypt
    return bcrypt.hash(password)


def _verify_hash(password: str, hashed: str) -> bool:
    from passlib.hash import bcrypt
    try:
        return bcrypt.verify(password, hashed)
    except Exception:
        return False


def ensure_table():
    execute("""
        CREATE TABLE IF NOT EXISTS panel_users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT,
            role TEXT DEFAULT 'staff',
            active BOOLEAN DEFAULT TRUE,
            club_id INT DEFAULT 1,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)


def bootstrap_superadmin():
    """Crea el superadmin la primera vez."""
    row = execute("SELECT COUNT(*) AS n FROM panel_users", fetch_one=True)
    if row and int(row["n"]) > 0:
        return
    pwd = os.getenv("SUPERADMIN_PASSWORD", "") or os.getenv("VOICE_API_KEY", "")
    if not pwd:
        logger.warning("No SUPERADMIN_PASSWORD/VOICE_API_KEY: superadmin no creado")
        return
    execute("""
        INSERT INTO panel_users (username, password_hash, name, role)
        VALUES ('alejandro', %s, 'Alejandro', 'superadmin')
        ON CONFLICT (username) DO NOTHING
    """, [_hash(pwd)])
    logger.info("👑 Superadmin 'alejandro' creado")


def login(username: str, password: str) -> dict | None:
    """Devuelve el usuario si las credenciales son válidas."""
    row = execute(
        "SELECT * FROM panel_users WHERE username=%s AND active=TRUE",
        [(username or "").strip().lower()], fetch_one=True)
    if row and _verify_hash(password or "", row["password_hash"]):
        return dict(row)
    return None


def make_token(user: dict) -> str:
    from jose import jwt
    payload = {
        "sub": user["username"],
        "name": user.get("name") or user["username"],
        "role": user.get("role") or "staff",
        "exp": datetime.utcnow() + timedelta(hours=SESSION_HOURS),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def parse_token(token: str) -> dict | None:
    from jose import jwt
    try:
        return jwt.decode(token, _secret(), algorithms=["HS256"])
    except Exception:
        return None


def current_user(request) -> dict | None:
    """Usuario de la sesión (cookie), o None."""
    token = request.cookies.get(COOKIE, "")
    if not token:
        return None
    return parse_token(token)


def create_user(username: str, password: str, name: str, role: str = "staff"):
    role = role if role in ("staff", "superadmin") else "staff"
    execute("""
        INSERT INTO panel_users (username, password_hash, name, role)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (username) DO UPDATE
        SET password_hash=EXCLUDED.password_hash, name=EXCLUDED.name,
            role=EXCLUDED.role, active=TRUE
    """, [(username or "").strip().lower(), _hash(password), name, role])


def set_active(username: str, active: bool):
    execute("UPDATE panel_users SET active=%s WHERE username=%s",
            [active, (username or "").strip().lower()])


def list_users() -> list[dict]:
    rows = execute(
        "SELECT username, name, role, active, created_at FROM panel_users ORDER BY created_at",
        fetch_all=True) or []
    return [dict(r) for r in rows]


def login_page(error: str = "") -> str:
    err = f'<p class="err">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Club de Padel Victoria — Panel</title>
<style>
body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background:#f3f4f6;
       display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
.card {{ background:white; border:1px solid #e5e7eb; border-radius:16px; padding:2.2rem;
        width:320px; box-shadow:0 10px 30px rgba(0,0,0,.06); }}
h1 {{ font-size:1.1rem; margin:0 0 0.3rem; }}
p.sub {{ color:#6b7280; font-size:0.8rem; margin:0 0 1.2rem; }}
label {{ font-size:0.8rem; color:#374151; display:block; margin-bottom:0.8rem; }}
input {{ width:100%; border:1px solid #d1d5db; border-radius:8px; padding:9px 10px;
        margin-top:4px; box-sizing:border-box; font-size:0.9rem; }}
button {{ width:100%; background:#111827; color:white; border:none; border-radius:8px;
         padding:10px; font-size:0.9rem; cursor:pointer; margin-top:0.4rem; }}
.err {{ color:#b02a2a; font-size:0.8rem; }}
</style></head><body>
<form class="card" method="post" action="/panel/login">
  <h1>🎾 Club de Padel Victoria</h1>
  <p class="sub">Panel de administración</p>
  {err}
  <label>Usuario<input name="username" autocomplete="username" required></label>
  <label>Contraseña<input name="password" type="password" autocomplete="current-password" required></label>
  <button type="submit">Entrar</button>
</form></body></html>"""
