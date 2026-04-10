"""
MatchBot - Database connection pool (psycopg2, same pattern as CotizaExpress)
"""
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from config.settings import settings

_pool = None


def init_db():
    """Initialize the connection pool. Call once at startup."""
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=20,
        dsn=settings.DATABASE_URL,
    )
    return _pool


def get_pool():
    global _pool
    if _pool is None:
        init_db()
    return _pool


@contextmanager
def get_conn():
    """Context manager: yields a connection, auto-returns to pool."""
    p = get_pool()
    conn = p.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


@contextmanager
def get_cursor(dict_cursor=True):
    """Context manager: yields a cursor with auto-commit."""
    with get_conn() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        with conn.cursor(cursor_factory=cursor_factory) as cur:
            yield cur


def execute(sql: str, params=None, fetch_one=False, fetch_all=False):
    """Execute SQL and optionally fetch results."""
    with get_cursor() as cur:
        cur.execute(sql, params)
        if fetch_one:
            return cur.fetchone()
        if fetch_all:
            return cur.fetchall()
        return cur.rowcount
