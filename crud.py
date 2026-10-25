# crud.py
from typing import Any, Dict, List, Optional, Tuple
from dependencies import get_db

# --------- Helpers to map sqlite3.Row -> dicts that your templates expect ---------

def _row_to_customer(row) -> Dict[str, Any]:
    """Map customers row to dict with template-friendly keys (including `_id`)."""
    if not row:
        return {}
    return {
        "_id": row["id"],
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "id_proof": row["id_proof"],
        "address": row["address"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def _row_to_booking(row):
    """Convert a sqlite3.Row into a clean dict with fallbacks."""
    def safe_get(col):
        return row[col] if col in row.keys() else None

    d= {
        "id": row["id"],
        "customer_id": row["customer_id"],
        "room_number": row["room_number"],
        "check_in": row["check_in"],
        "expected_check_out": row["expected_check_out"],
        "actual_check_out": row["actual_check_out"],
        "room_rate": row["room_rate"],
        "num_nights": row["num_nights"],
        "extra_charges": row["extra_charges"],
        "total_amount": row["total_amount"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],

        # Optional columns — safely access with fallback
        "customer_name": safe_get("customer_name"),
        "email": safe_get("email"),
        "phone": safe_get("phone"),
        "pax": safe_get("pax"),
        "reference_id": safe_get("reference_id"),
        "reference_name": safe_get("reference_name"),
    }
    d["_id"] = d["id"]
    return d

# --------- Dashboard stats used by templates/dashboard.html ---------

def get_dashboard_stats() -> Dict[str, int]:
    with get_db() as db:
        db.execute("SELECT COUNT(*) AS c FROM rooms")
        total_rooms = (db.fetchone() or {"c": 0})["c"]

        db.execute("SELECT COUNT(*) AS c FROM rooms WHERE status = 'Occupied'")
        occupied_rooms = (db.fetchone() or {"c": 0})["c"]

        db.execute("SELECT COUNT(*) AS c FROM rooms WHERE status = 'Available'")
        available_rooms = (db.fetchone() or {"c": 0})["c"]

        db.execute("SELECT COUNT(*) AS c FROM bookings WHERE status = 'Booked'")
        active_bookings = (db.fetchone() or {"c": 0})["c"]

    return {
        "total_rooms": total_rooms,
        "occupied_rooms": occupied_rooms,
        "available_rooms": available_rooms,
        "active_bookings": active_bookings,
    }

# --------- Customers (list/search/detail/add/update) ---------

def list_customers(q: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db() as db:
        if q:
            like = f"%{q.strip()}%"
            db.execute(
                """
                SELECT * FROM customers
                WHERE name LIKE ? OR email LIKE ? OR phone LIKE ?
                ORDER BY created_at DESC
                """,
                (like, like, like),
            )
        else:
            db.execute(
                "SELECT * FROM customers ORDER BY created_at DESC"
            )
        rows = db.fetchall()
    return [_row_to_customer(r) for r in rows]

def get_customer(customer_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as db:
        db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,))
        row = db.fetchone()
    return _row_to_customer(row) if row else None

def get_customer_bookings(customer_id: int) -> List[Dict[str, Any]]:
    with get_db() as db:
        db.execute(
            """
            SELECT b.*
            FROM bookings b
            WHERE b.customer_id = ?
            ORDER BY COALESCE(b.updated_at, b.created_at) DESC
            """,
            (customer_id,),
        )
        rows = db.fetchall()
    return [_row_to_booking(r) for r in rows]

def add_customer(
    name: str,
    email: Optional[str],
    phone: Optional[str],
    id_proof: Optional[str],
    address: Optional[str],
    customer_type: Optional[str] = None,
) -> int:
    from datetime import datetime
    now = datetime.utcnow().isoformat()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO customers (name, email, phone, id_proof, address, customer_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name.strip(), (email or "").strip() or None, (phone or "").strip() or None,
             (id_proof or "").strip() or None, (address or "").strip() or None, (customer_type or "").strip() or None, now, now),
        )
        db.execute("SELECT last_insert_rowid() AS lid")
        rid = (db.fetchone() or {"lid": 0})["lid"]
    return int(rid or 0)

def update_customer(
    customer_id: int,
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    id_proof: Optional[str] = None,
    address: Optional[str] = None,
    customer_type: Optional[str] = None,
) -> None:
    from datetime import datetime
    fields: List[Tuple[str, Any]] = []
    if name is not None:           fields.append(("name", name.strip()))
    if email is not None:          fields.append(("email", email.strip()))
    if phone is not None:          fields.append(("phone", phone.strip()))
    if id_proof is not None:       fields.append(("id_proof", id_proof.strip()))
    if address is not None:        fields.append(("address", address.strip()))
    if customer_type is not None:  fields.append(("customer_type", customer_type.strip()))
    fields.append(("updated_at", datetime.utcnow().isoformat()))

    set_clause = ", ".join([f"{k} = ?" for k, _ in fields])
    params = [v for _, v in fields] + [customer_id]

    with get_db() as db:
        db.execute(f"UPDATE customers SET {set_clause} WHERE id = ?", params)

# --------- Rooms (fetch all for booking creation) ---------

def get_all_rooms() -> List[Dict[str, Any]]:
    """Fetch all available rooms for booking form dropdown."""
    with get_db() as db:
        db.execute("""
            SELECT id, room_number, room_type, price_per_night, status, housekeeping_status
            FROM rooms
            ORDER BY room_number ASC
        """)
        rows = db.fetchall()
    return [
        {
            "id": r["id"],
            "room_number": r["room_number"],
            "room_type": r["room_type"],
            "price_per_night": r["price_per_night"],
            "status": r["status"],
            "housekeeping_status": r["housekeeping_status"],
        }
        for r in rows
    ]
