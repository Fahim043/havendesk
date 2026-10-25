# dependencies.py
import os
import sqlite3
from contextlib import contextmanager
from fastapi import Request, HTTPException, status
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("SQLITE_DB_PATH", "hotel_management.db")

async def require_auth(request: Request):
    """
    Dependency that ensures a user is logged in and session is not expired.
    Also updates last_activity on each valid request.
    """
    import time
    
    SESSION_TIMEOUT = 600  # 10 minutes in seconds
    
    user = request.session.get("user")
    last_activity = request.session.get("last_activity", 0)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Not authenticated",
            headers={"Location": "/auth/login"}
        )
    
    # Check inactivity timeout ONLY if last_activity was set previously
    # (If last_activity is 0, it means it's a fresh session - don't expire it)
    if last_activity > 0 and (time.time() - last_activity > SESSION_TIMEOUT):
        request.session.clear()  # Expire the session
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Session expired due to inactivity",
            headers={"Location": "/auth/login"}
        )
    
    # Update last activity timestamp
    request.session["last_activity"] = time.time()
    
    return user

def _run_migrations(conn: sqlite3.Connection):
    """Apply any pending schema migrations safely (idempotent)."""
    cur = conn.cursor()
    migrations = [
        # customers: add customer_type column
        ("customers", "customer_type", "ALTER TABLE customers ADD COLUMN customer_type TEXT"),
        # customers: add loyalty_points column (future-proofing)
        ("customers", "loyalty_points", "ALTER TABLE customers ADD COLUMN loyalty_points INTEGER DEFAULT 0"),
        # bookings: add payment_method column (if missing)
        ("bookings", "payment_method", "ALTER TABLE bookings ADD COLUMN payment_method TEXT"),
        # bookings: add pax column
        ("bookings", "pax", "ALTER TABLE bookings ADD COLUMN pax INTEGER DEFAULT 1"),
        # bookings: add reference_id column
        ("bookings", "reference_id", "ALTER TABLE bookings ADD COLUMN reference_id INTEGER"),
        # customers: marketing opt-out flag (campaigns skip opted-out customers)
        ("customers", "marketing_opt_out", "ALTER TABLE customers ADD COLUMN marketing_opt_out INTEGER DEFAULT 0"),
        # campaigns module tables
        ("campaign_settings", None, """
            CREATE TABLE IF NOT EXISTS campaign_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """),
        ("campaigns", None, """
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                channel TEXT NOT NULL,
                wa_mode TEXT,
                subject TEXT,
                body TEXT,
                filters_json TEXT,
                status TEXT,
                created_at TEXT,
                completed_at TEXT
            )
        """),
        ("campaign_recipients", None, """
            CREATE TABLE IF NOT EXISTS campaign_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                customer_id INTEGER,
                name TEXT,
                channel TEXT,
                address TEXT,
                subject TEXT,
                message TEXT,
                status TEXT,
                error TEXT,
                sent_at TEXT
            )
        """),
        # settings table
        ("settings", None, """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """),
    ]
    for table, column, sql in migrations:
        try:
            if column:
                # Check if column already exists
                cur.execute(f"PRAGMA table_info({table})")
                cols = [r[1] for r in cur.fetchall()]
                if column not in cols:
                    cur.execute(sql)
            else:
                cur.execute(sql)
        except Exception:
            pass  # Column or table already exists
    conn.commit()


def get_connection() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"❌ Database not found at {DB_PATH}. Run init_db_sqlite.py first."
        )
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _run_migrations(conn)
    return conn

@contextmanager
def get_db():
    """
    Usage:
        with get_db() as db:
            db.execute("SQL", params)
            rows = db.fetchall()
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()