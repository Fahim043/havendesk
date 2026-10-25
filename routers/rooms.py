# routers/rooms.py
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from dependencies import get_db

from fastapi.templating import Jinja2Templates
from datetime import datetime
import sqlite3

templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now
router = APIRouter()

# Default room types — always available
DEFAULT_ROOM_TYPES = ["VVIP", "VIP", "Suite", "Couple Bed", "Double Bed", "Semi-Double", "Single"]

@router.get("/types")
def get_room_types():
    """Return all distinct room types (defaults + custom ones from DB)."""
    from fastapi.responses import JSONResponse
    with get_db() as db:
        db.execute("SELECT DISTINCT room_type FROM rooms ORDER BY room_type")
        db_types = [r["room_type"] for r in db.fetchall()]
    
    # Merge defaults + DB types, preserving order
    all_types = list(DEFAULT_ROOM_TYPES)
    for t in db_types:
        if t not in all_types:
            all_types.append(t)
    
    return JSONResponse({"types": all_types})

@router.get("/")
def list_rooms(
    request: Request,
    search: str = "",
    filter_type: str = "",
    filter_status: str = "",
    filter_housekeeping: str = "",
    sort_by: str = "room_number",
    order: str = "asc"
):
    query = "SELECT * FROM rooms WHERE 1=1"
    params = []

    if search:
        query += " AND room_number LIKE ?"
        params.append(f"%{search}%")
    if filter_type:
        query += " AND room_type = ?"
        params.append(filter_type)
    if filter_status:
        query += " AND status = ?"
        params.append(filter_status)
    if filter_housekeeping:
        query += " AND housekeeping_status = ?"
        params.append(filter_housekeeping)

    # Sorting
    allowed_sort_cols = {"room_number", "room_type", "price_per_night", "status"}
    if sort_by not in allowed_sort_cols:
        sort_by = "room_number"
    
    # Safe order direction
    if order.lower() == "desc":
        direction = "DESC"
    else:
        direction = "ASC"

    # Special case for room_number to sort numerically
    if sort_by == "room_number":
        query += f" ORDER BY CAST(room_number as INTEGER) {direction}, room_number {direction}"
    else:
        query += f" ORDER BY {sort_by} {direction}"

    with get_db() as db:
        db.execute(query, tuple(params))
        rooms = db.fetchall()
    
    return templates.TemplateResponse("rooms.html", {
        "request": request, 
        "filtered_rooms": rooms,
        "search": search,
        "filter_type": filter_type,
        "filter_status": filter_status,
        "filter_housekeeping": filter_housekeeping,
        "currency": __import__('routers.settings').settings.get_setting("currency_symbol", "$")
    })

@router.post("/add")
def add_room(
    room_number: str = Form(...),
    room_type: str = Form("Double Bed"),
    price_per_night: float = Form(...),
    status: str = Form("Available"),
    housekeeping_status: str = Form("Clean"),
):
    try:
        with get_db() as db:
            db.execute("""
                INSERT INTO rooms (room_number, room_type, price_per_night, status, housekeeping_status)
                VALUES (?, ?, ?, ?, ?)
            """, (room_number.strip(), room_type.strip(), price_per_night, status.strip(), housekeeping_status.strip()))
        return {"success": True, "message": "Room added successfully"}
    except sqlite3.IntegrityError:
        return {"success": False, "message": "Room Already Exists"}

@router.post("/housekeeping")
def set_housekeeping(
    room_number: str = Form(...),
    housekeeping_status: str = Form(...)
):
    hs = housekeeping_status.strip()
    rn = room_number.strip()
    is_oos = hs in ("Out of Service", "OutOfService")
    is_inuse = hs == "InUse"
    new_status = None  # track what room status becomes
    with get_db() as db:
        db.execute("SELECT status FROM rooms WHERE room_number = ?", (rn,))
        row = db.fetchone()
        old_status = row["status"] if row else "Available"

        if is_oos:
            new_status = "OutOfService"
        elif is_inuse:
            new_status = "Occupied"
        elif old_status in ("OutOfService", "Occupied") and hs in ("Clean", "Dirty"):
            # Restoring from OOS/InUse → Available (only if not genuinely occupied by booking)
            new_status = "Available"
        else:
            new_status = old_status  # keep current

        db.execute("""
            UPDATE rooms
            SET housekeeping_status = ?, status = ?
            WHERE room_number = ?
        """, (hs, new_status, rn))
    return {"success": True, "room_number": room_number, "housekeeping_status": housekeeping_status, "status": new_status}

@router.post("/update")
def update_room(
    original_room_number: str = Form(...),
    room_number: str = Form(...),
    room_type: str = Form(...),
    price_per_night: float = Form(...),
    status: str = Form(...),
    housekeeping_status: str = Form(...),
):
    new_num = room_number.strip()
    orig_num = original_room_number.strip()
    hs = housekeeping_status.strip()
    st = status.strip()
    is_oos = hs in ("Out of Service", "OutOfService")
    is_inuse = hs == "InUse"

    # Auto-sync: if housekeeping is Out of Service, force status to OutOfService
    if is_oos:
        st = "OutOfService"
    elif is_inuse:
        st = "Occupied"
    # If housekeeping is NOT Out of Service but status was OutOfService, restore to Available
    elif st in ("OutOfService",) and not is_oos:
        st = "Available"

    # If room number changed, check for conflicts
    if new_num != orig_num:
        with get_db() as db:
            db.execute("SELECT id FROM rooms WHERE room_number = ?", (new_num,))
            if db.fetchone():
                return {"success": False, "message": "Room Already Exists"}

    try:
        with get_db() as db:
            db.execute("""
                UPDATE rooms
                SET room_number = ?, room_type = ?, price_per_night = ?, status = ?, housekeeping_status = ?
                WHERE room_number = ?
            """, (new_num, room_type.strip(), price_per_night, st, hs, orig_num))
        return {"success": True, "message": "Room updated successfully"}
    except sqlite3.IntegrityError:
        return {"success": False, "message": "Room Already Exists"}

@router.post("/set_status")
def set_status(
    room_number: str = Form(...),
    status: str = Form(...)
):
    with get_db() as db:
        db.execute("""
            UPDATE rooms
            SET status = ?
            WHERE room_number = ?
        """, (status.strip(), room_number.strip()))
    return RedirectResponse("/rooms", status_code=303)