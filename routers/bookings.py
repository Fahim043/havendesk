# routers/bookings.py
from fastapi import APIRouter, Request, Form, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from dependencies import get_db
from typing import Any
from datetime import datetime, timedelta
import json
from routers.settings import get_setting


router = APIRouter()

templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now

# ---------- utilities ----------
def _dt_parse(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None

# Helper for automatic financial tracking
def _create_financial_transaction(db, type_val, amount, category, description, related_booking_id=None):
    """
    Creates a financial transaction automatically.
    """
    from datetime import datetime
    now = datetime.now().isoformat()
    db.execute("""
        INSERT INTO financial_transactions (type, amount, category, description, date)
        VALUES (?, ?, ?, ?, ?)
    """, (type_val, amount, category, description, now))


def _row_to_booking_dict(row: dict):
    charges = []
    raw = row.get("extra_charges")
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, list):
                for c in obj:
                    charges.append({
                        "description": str(c.get("description", c.get("title", ""))),
                        "amount": float(c.get("amount", 0))
                    })
        except Exception:
            pass

    return {
        "_id": row["id"],
        "id": row["id"],
        "customer_id": row["customer_id"],
        "customer_name": row.get("customer_name"),
        "room_number": row["room_number"],
        "check_in": _dt_parse(row["check_in"]),
        "expected_check_out": _dt_parse(row["expected_check_out"]),
        "actual_check_out": _dt_parse(row["actual_check_out"]),
        "room_rate": float(row["room_rate"]),
        "num_nights": int(row["num_nights"]),
        "extra_charges": charges,
        "total_amount": float(row["total_amount"]) if row["total_amount"] is not None else None,
        "advance_amount": float(row.get("advance_amount") or 0),
        "amount_paid": float(row.get("amount_paid") or 0),
        "payment_status": row.get("payment_status") or "Unpaid",
        "group_id": row.get("group_id"),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "pax": int(row.get("pax") or 1),
        "reference_id": row.get("reference_id"),
        "reference_name": row.get("reference_name"),
    }

def _recompute_total(room_rate: float, num_nights: int, extra_charges_json: str | None) -> float:
    extra_total = 0.0
    if extra_charges_json:
        try:
            for c in json.loads(extra_charges_json) or []:
                extra_total += float(c.get("amount", 0))
        except Exception:
            pass
    subtotal = float(room_rate) * int(num_nights) + float(extra_total)
    
    # Calculate tax
    from routers.settings import get_setting
    try:
        tax_rate = float(get_setting("tax_percent", "10")) / 100.0
    except (ValueError, TypeError):
        tax_rate = 0.10
        
    tax_amount = round(subtotal * tax_rate, 2)
    grand_total = round(subtotal + tax_amount, 2)
    
    return grand_total

def _room_rate_and_status(db, room_number: str) -> tuple[float, str]:
    db.execute("SELECT price_per_night, status FROM rooms WHERE room_number = ?", (room_number,))
    r = db.fetchone()
    if not r:
        raise ValueError("Room not found")
    return float(r["price_per_night"]), r["status"]

def _mark_room_status(db, room_number: str, status: str, housekeeping: str | None = None):
    if housekeeping is None:
        db.execute("UPDATE rooms SET status = ? WHERE room_number = ?", (status, room_number))
    else:
        db.execute(
            "UPDATE rooms SET status = ?, housekeeping_status = ? WHERE room_number = ?",
            (status, housekeeping, room_number),
        )

def _find_customer_by_email_or_phone(db, email: str | None, phone: str | None):
    # Prefer email (unique), else phone.
    if email:
        db.execute("SELECT * FROM customers WHERE LOWER(email) = LOWER(?)", (email.strip(),))
        row = db.fetchone()
        if row:
            return row
    if phone:
        db.execute("SELECT * FROM customers WHERE phone = ?", (phone.strip(),))
        row = db.fetchone()
        if row:
            return row
    return None

def _create_customer(db, name, email, phone, id_proof, address) -> int:
    now = datetime.now().isoformat()
    db.execute("""
        INSERT INTO customers (name, email, phone, id_proof, address, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (name, email, phone, id_proof, address, now, now))
    db.execute("SELECT last_insert_rowid() AS lid")
    return int((db.fetchone() or {"lid": 0})["lid"])


def check_room_availability(db, room_number: str, check_in: str, check_out: str, exclude_booking_id: int = None) -> dict:
    """
    Check if a room is available for the given date range.
    Returns: {"available": bool, "conflict": booking_dict or None, "message": str}
    
    A conflict occurs when:
    - An existing booking (not Checked-out) overlaps with the requested dates
    - Overlap = NOT (requested_check_out <= existing_check_in OR requested_check_in >= existing_check_out)
    """
    from datetime import datetime
    
    # Parse requested dates
    try:
        req_in = datetime.fromisoformat(check_in[:10]) if isinstance(check_in, str) else check_in
        req_out = datetime.fromisoformat(check_out[:10]) if isinstance(check_out, str) else check_out
    except:
        return {"available": False, "conflict": None, "message": "Invalid date format"}
    
    # Get all non-checked-out bookings for this room
    query = """
        SELECT id, check_in, expected_check_out, status, customer_id
        FROM bookings 
        WHERE room_number = ? AND status NOT IN ('Checked-out', 'Cancelled')
    """
    params = [room_number]
    
    if exclude_booking_id:
        query += " AND id != ?"
        params.append(exclude_booking_id)
    
    db.execute(query, params)
    bookings = db.fetchall()
    
    for b in bookings:
        try:
            existing_in = datetime.fromisoformat(b["check_in"][:10]) if b["check_in"] else None
            existing_out = datetime.fromisoformat(b["expected_check_out"][:10]) if b["expected_check_out"] else None
        except:
            continue
        
        if not existing_in or not existing_out:
            continue
        
        # Check for overlap: NOT (req_out <= existing_in OR req_in >= existing_out)
        # Overlap exists if: req_in < existing_out AND req_out > existing_in
        if req_in < existing_out and req_out > existing_in:
            return {
                "available": False,
                "conflict": dict(b),
                "message": f"Room {room_number} is already booked from {existing_in.strftime('%b %d')} to {existing_out.strftime('%b %d')} (Booking #{b['id']})"
            }
    
    return {"available": True, "conflict": None, "message": "Room is available"}

def _maybe_update_customer_profile(db, customer_row, name, email, phone, id_proof, address):
    """Update existing customer if we got new non-empty values that differ."""
    updates = {}
    if name and name.strip() and name.strip() != (customer_row["name"] or ""):
        updates["name"] = name.strip()
    if email and email.strip() and (customer_row["email"] or "").lower() != email.strip().lower():
        # Only update email if empty in DB to avoid UNIQUE conflicts
        if not customer_row["email"]:
            updates["email"] = email.strip()
    if phone and phone.strip() and phone.strip() != (customer_row["phone"] or ""):
        updates["phone"] = phone.strip()
    if id_proof and id_proof.strip() and id_proof.strip() != (customer_row["id_proof"] or ""):
        updates["id_proof"] = id_proof.strip()
    if address and address.strip() and address.strip() != (customer_row["address"] or ""):
        updates["address"] = address.strip()

    if updates:
        sets = ", ".join([f"{k} = ?" for k in updates.keys()])
        params = list(updates.values())
        params.append(customer_row["id"])
        db.execute(f"UPDATE customers SET {sets}, updated_at = ? WHERE id = ?", (*list(updates.values()), datetime.now().isoformat(), customer_row["id"]))

# ---------- pages ----------
@router.get("/", response_class=HTMLResponse)
def list_bookings(
    request: Request, 
    page: int = 1, 
    limit: int = 20, 
    search: str = "", 
    status: str = "",
    sort_by: str = "id",
    order: str = "desc"
):
    offset = (page - 1) * limit
    today = datetime.now().strftime('%Y-%m-%d')
    three_days_later = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
    
    with get_db() as db:
        # Today's checkouts (urgently need checkout today)
        db.execute("""
            SELECT b.*, c.name AS customer_name, ref.name AS reference_name
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            LEFT JOIN customers ref ON ref.id = b.reference_id
            WHERE b.status IN ('Booked', 'Active')
            AND DATE(b.expected_check_out) = DATE(?)
            ORDER BY b.expected_check_out ASC
        """, (today,))
        todays_checkout_rows = db.fetchall()
        todays_checkouts = [_row_to_booking_dict(dict(r)) for r in todays_checkout_rows]
        
        # Upcoming checkouts (next 3 days, excluding today)
        db.execute("""
            SELECT b.*, c.name AS customer_name, ref.name AS reference_name
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            LEFT JOIN customers ref ON ref.id = b.reference_id
            WHERE b.status IN ('Booked', 'Active')
            AND DATE(b.expected_check_out) > DATE(?)
            AND DATE(b.expected_check_out) <= DATE(?)
            ORDER BY b.expected_check_out ASC
        """, (today, three_days_later))
        upcoming_checkout_rows = db.fetchall()
        upcoming_checkouts = [_row_to_booking_dict(dict(r)) for r in upcoming_checkout_rows]
        
        # Today's expected check-ins (reservations with check_in date = today)
        db.execute("""
            SELECT b.*, c.name AS customer_name, ref.name AS reference_name
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            LEFT JOIN customers ref ON ref.id = b.reference_id
            WHERE b.status = 'Reserved'
            AND DATE(b.check_in) = DATE(?)
            ORDER BY b.check_in ASC
        """, (today,))
        todays_checkin_rows = db.fetchall()
        todays_checkins = [_row_to_booking_dict(dict(r)) for r in todays_checkin_rows]
        
        # Late check-ins (reservations where reserved date has passed, guest hasn't arrived)
        db.execute("""
            SELECT b.*, c.name AS customer_name, ref.name AS reference_name
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            LEFT JOIN customers ref ON ref.id = b.reference_id
            WHERE b.status = 'Reserved'
            AND DATE(b.check_in) < DATE(?)
            ORDER BY b.check_in ASC
        """, (today,))
        late_checkin_rows = db.fetchall()
        late_checkins = [_row_to_booking_dict(dict(r)) for r in late_checkin_rows]
        
        # Base query for all bookings
        where_clause = "WHERE 1=1"
        params = []
        
        if search:
            where_clause += " AND (c.name LIKE ? OR b.room_number LIKE ? OR CAST(b.id AS TEXT) LIKE ?)"
            s = f"%{search}%"
            params.extend([s, s, s])
            
        if status:
            where_clause += " AND b.status = ?"
            params.append(status)

        # Get count
        db.execute(f"""
            SELECT COUNT(*) as c 
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            {where_clause}
        """, params)
        total_count = db.fetchone()['c']
        
        # Sorting
        allowed_sort_cols = {
            "id": "b.id",
            "customer_name": "c.name",
            "check_in": "b.check_in",
            "expected_check_out": "b.expected_check_out",
            "total_amount": "b.total_amount",
            "created_at": "b.created_at"
        }
        
        sort_col = allowed_sort_cols.get(sort_by, "b.check_in")
        direction = "DESC" if order.lower() == "desc" else "ASC"
        
        # Get data
        db.execute(f"""
            SELECT b.*,
                   c.name AS customer_name,
                   ref.name AS reference_name
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            LEFT JOIN customers ref ON ref.id = b.reference_id
            {where_clause}
            ORDER BY {sort_col} {direction}
            LIMIT ? OFFSET ?
        """, (*params, limit, offset))
        rows = db.fetchall()
        
    bookings = [_row_to_booking_dict(dict(r)) for r in rows]
    total_pages = (total_count + limit - 1) // limit
    
    # Get services catalog for Add Charge dropdown
    with get_db() as db:
        db.execute("SELECT id, name, price, category FROM services_catalog ORDER BY name")
        services_rows = db.fetchall()
        services_catalog = [dict(r) for r in services_rows]
    
    return templates.TemplateResponse("bookings.html", {
        "request": request, 
        "bookings": bookings,
        "todays_checkouts": todays_checkouts,
        "upcoming_checkouts": upcoming_checkouts,
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "search": search,
        "status": status,
        "services_catalog": services_catalog,
        "todays_checkins": todays_checkins,
        "late_checkins": late_checkins,
        "currency": get_setting("currency_symbol", "$")
    })

# ---------- data: available rooms for dropdown ----------
@router.get("/available-rooms")
def available_rooms(nights: int = Query(0)):
    with get_db() as db:
        db.execute("""
            SELECT room_number, room_type, price_per_night, status
            FROM rooms
            ORDER BY CAST(room_number AS INTEGER), room_number
        """)
        rows = db.fetchall()
        
        result = []
        for r in rows:
            room_data = {
                "room_number": r["room_number"],
                "room_type": r["room_type"],
                "price_per_night": float(r["price_per_night"]),
                "status": r["status"],
                "has_conflict": False,
                "conflict_message": ""
            }
            
            # If nights provided, check for future reservation conflicts
            if nights > 0 and r["status"] == "Available":
                now = datetime.now()
                check_out = now + timedelta(days=nights)
                avail = check_room_availability(
                    db, r["room_number"],
                    now.isoformat(), check_out.isoformat()
                )
                if not avail["available"]:
                    room_data["has_conflict"] = True
                    room_data["conflict_message"] = avail["message"]
            
            result.append(room_data)
    
    return JSONResponse(result)

# ---------- data: rooms available for specific date range ----------
@router.get("/rooms-for-dates")
def rooms_for_dates(
    check_in: str = Query(...),
    check_out: str = Query(...)
):
    """Return all rooms with availability info for a date range.
    Used by the reservation form to allow booking occupied rooms for future dates.
    """
    with get_db() as db:
        db.execute("""
            SELECT room_number, room_type, price_per_night, status
            FROM rooms
            WHERE status != 'Out of Service'
            ORDER BY CAST(room_number AS INTEGER), room_number
        """)
        rooms = db.fetchall()
        
        result = []
        for r in rooms:
            avail = check_room_availability(db, r["room_number"], check_in, check_out)
            result.append({
                "room_number": r["room_number"],
                "room_type": r["room_type"],
                "price_per_night": float(r["price_per_night"]),
                "status": r["status"],
                "available_for_dates": avail["available"],
                "conflict_message": avail.get("message", "") if not avail["available"] else ""
            })
    
    return JSONResponse(result)

# ---------- data: customer lookup by email or phone ----------
@router.get("/customer-lookup")
def customer_lookup(email: str | None = Query(None), phone: str | None = Query(None)):
    email = (email or "").strip() or None
    phone = (phone or "").strip() or None
    with get_db() as db:
        row = _find_customer_by_email_or_phone(db, email, phone)
        if not row:
            return JSONResponse({"exists": False})
        return JSONResponse({
            "exists": True,
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "phone": row["phone"],
            "id_proof": row["id_proof"],
            "address": row["address"],
        })
    

# routers/bookings.py  (add)

@router.get("/customer-analytics")
def customer_analytics(customer_id: int):
    """
    Compact analytics for a returning customer:
    - lifetime_value
    - total_stays
    - avg_nights
    - favorite_room_type
    - recent_bookings (last 5)
    - last_stay summary
    """
    with get_db() as db:
        # Lifetime value & total stays & avg nights
        db.execute("""
            SELECT 
                COUNT(*) AS total_stays,
                COALESCE(SUM(total_amount), 0) AS lifetime_value,
                COALESCE(AVG(num_nights), 0) AS avg_nights
            FROM bookings
            WHERE customer_id = ?
        """, (customer_id,))
        agg = db.fetchone() or {"total_stays": 0, "lifetime_value": 0, "avg_nights": 0}
        total_stays = int(agg["total_stays"])
        lifetime_value = float(agg["lifetime_value"] or 0)
        avg_nights = float(agg["avg_nights"] or 0)

        # Favorite room type (by count)
        db.execute("""
            SELECT r.room_type, COUNT(*) AS cnt
            FROM bookings b
            LEFT JOIN rooms r ON r.room_number = b.room_number
            WHERE b.customer_id = ?
            GROUP BY r.room_type
            ORDER BY cnt DESC
            LIMIT 1
        """, (customer_id,))
        fav = db.fetchone()
        favorite_room_type = fav["room_type"] if fav and fav["room_type"] else None

        # Last stay (most recent check-in)
        db.execute("""
            SELECT b.id, b.room_number, b.check_in, b.expected_check_out, b.actual_check_out, 
                   b.status, b.total_amount
            FROM bookings b
            WHERE b.customer_id = ?
            ORDER BY COALESCE(b.actual_check_out, b.expected_check_out, b.check_in) DESC
            LIMIT 1
        """, (customer_id,))
        last = db.fetchone()

        def _row_to_brief(x: Any) -> dict:
            if not x:
                return {}
            return {
                "id": x["id"],
                "room_number": x["room_number"],
                "check_in": x["check_in"],
                "expected_check_out": x["expected_check_out"],
                "actual_check_out": x["actual_check_out"],
                "status": x["status"],
                "total_amount": float(x["total_amount"] or 0),
            }

        last_stay = _row_to_brief(last)

        # Recent bookings (last 5)
        db.execute("""
            SELECT b.id, b.room_number, b.check_in, b.expected_check_out, b.actual_check_out, 
                   b.status, b.total_amount, r.room_type
            FROM bookings b
            LEFT JOIN rooms r ON r.room_number = b.room_number
            WHERE b.customer_id = ?
            ORDER BY COALESCE(b.actual_check_out, b.expected_check_out, b.check_in) DESC
            LIMIT 5
        """, (customer_id,))
        rows = db.fetchall()
        recent_bookings = [{
            "id": r["id"],
            "room_number": r["room_number"],
            "room_type": r["room_type"],
            "check_in": r["check_in"],
            "expected_check_out": r["expected_check_out"],
            "actual_check_out": r["actual_check_out"],
            "status": r["status"],
            "total_amount": float(r["total_amount"] or 0),
        } for r in rows]

    return {
        "total_stays": total_stays,
        "lifetime_value": lifetime_value,
        "avg_nights": round(avg_nights, 2),
        "favorite_room_type": favorite_room_type,
        "last_stay": last_stay,
        "recent_bookings": recent_bookings,
    }

# ---------- actions ----------
@router.post("/checkin")
def checkin(
    request: Request,
    # customer info (email/phone first for lookup)
    email: str = Form(None),
    phone: str = Form(None),
    customer_name: str = Form(None),
    id_proof: str = Form(None),
    address: str = Form(None),

    # booking info
    room_number: str = Form(...),
    nights: int = Form(...),
    payment_method: str = Form(""),  # optional unless advance paid
    selected_services: list[str] = Form([]), # IDs from service catalog
    advance_amount: float = Form(0.0),
    
    # Reservation support
    is_reservation: bool = Form(False),
    check_in_date: str = Form(None),

    # Discount during booking
    discount_type: str = Form(""),
    discount_value: float = Form(0.0),
    discount_reason: str = Form(""),
    
    # Pax and Reference
    pax: int = Form(1),
    reference_id: int = Form(None),
):
    now = datetime.now()
    try:
        nights = max(1, int(nights))
        room_number = room_number.strip()
        advance_amount = float(advance_amount) if advance_amount else 0.0

        with get_db() as db:
            # 1) Find existing customer by email or phone
            existing = _find_customer_by_email_or_phone(db, email, phone)

            if existing:
                # Optionally refresh profile with new data entered at check-in
                _maybe_update_customer_profile(db, existing, customer_name, email, phone, id_proof, address)
                customer_id = existing["id"]
            else:
                # Require at least a name for new customer
                if not (customer_name and customer_name.strip()):
                    raise ValueError("Customer name is required for a new customer.")
                customer_id = _create_customer(db,
                                               (customer_name or "").strip(),
                                               (email or "").strip() or None,
                                               (phone or "").strip() or None,
                                               (id_proof or "").strip() or None,
                                               (address or "").strip() or None)

            if reference_id and int(reference_id) == int(customer_id):
                raise ValueError("A guest cannot set themselves as a reference.")

            # 2) Validate room
            rate, status = _room_rate_and_status(db, room_number)
            if status == "OutOfService":
                error_msg = f"Room {room_number} is out of service."
                from urllib.parse import quote
                return RedirectResponse(f"/bookings/create?error={quote(error_msg)}", status_code=303)
            
            if status == "Occupied" and not is_reservation:
                # Walk-in: room must be available right now
                error_msg = f"Room {room_number} is occupied."
                from urllib.parse import quote
                return RedirectResponse(f"/bookings/create?error={quote(error_msg)}", status_code=303)
            
            if status == "Occupied" and is_reservation and check_in_date:
                # Reservation: allow if dates don't conflict
                try:
                    res_in = datetime.fromisoformat(check_in_date)
                except:
                    res_in = datetime.strptime(check_in_date, '%Y-%m-%d')
                res_out = res_in + timedelta(days=nights)
                avail = check_room_availability(db, room_number, res_in.isoformat(), res_out.isoformat())
                if not avail["available"]:
                    error_msg = f"Room {room_number} conflicts: {avail['message']}"
                    from urllib.parse import quote
                    return RedirectResponse(f"/bookings/create?error={quote(error_msg)}", status_code=303)

            # 3a) Calculate check-in and check-out dates
            # For reservations, use provided check_in_date; otherwise use now
            from routers.settings import get_setting
            checkout_time_str = get_setting("checkout_time", "11:00")
            try:
                ch_hour, ch_min = map(int, checkout_time_str.split(':'))
            except:
                ch_hour, ch_min = 11, 0
            
            if is_reservation and check_in_date:
                # Future reservation - parse the provided date
                try:
                    check_in_dt = datetime.fromisoformat(check_in_date)
                except:
                    check_in_dt = datetime.strptime(check_in_date, '%Y-%m-%d')
                
                # Prevent past-date reservations
                if check_in_dt.date() < now.date():
                    from urllib.parse import quote
                    error_msg = quote(f"Reservation date ({check_in_dt.strftime('%Y-%m-%d')}) cannot be in the past.")
                    return RedirectResponse(f"/bookings/create?error={error_msg}", status_code=303)
                
                check_in = check_in_dt.isoformat()
                checkout_date = check_in_dt + timedelta(days=nights)
                booking_status = 'Reserved'
            else:
                # Immediate check-in
                check_in = now.isoformat()
                check_in_dt = now
                checkout_date = now + timedelta(days=nights)
                booking_status = 'Booked'
            
            expected_out_dt = checkout_date.replace(hour=ch_hour, minute=ch_min, second=0, microsecond=0)
            expected_out = expected_out_dt.isoformat()
            
            # 3b) Check for room availability conflicts
            availability = check_room_availability(db, room_number, check_in, expected_out)
            if not availability["available"]:
                # Redirect back to create page with error message
                from urllib.parse import quote
                error_msg = quote(availability["message"])
                return RedirectResponse(f"/bookings/create?error={error_msg}", status_code=303)
            
            # 3b) Process Services
            initial_charges = []
            if selected_services:
                # Fetch services from DB
                placeholders = ','.join('?' for _ in selected_services)
                db.execute(f"SELECT * FROM services_catalog WHERE id IN ({placeholders})", selected_services)
                db_services = db.fetchall()
                
                for svc in db_services:
                    initial_charges.append({
                        "description": svc["name"],
                        "amount": svc["price"]
                    })
            
            # 3c) Total calculation
            subtotal = float(rate) * int(nights)
            for c in initial_charges:
                subtotal += float(c["amount"])

            # Apply discount if provided
            if discount_type and discount_value and discount_value > 0:
                if discount_type == "percentage":
                    room_subtotal = float(rate) * int(nights)
                    disc_amount = round(room_subtotal * (discount_value / 100), 2)
                    disc_desc = f"Discount ({discount_value}%)"
                else:
                    disc_amount = round(float(discount_value), 2)
                    disc_desc = f"Discount (Fixed)"
                if discount_reason:
                    disc_desc += f" - {discount_reason}"
                initial_charges.append({
                    "description": disc_desc,
                    "amount": -abs(disc_amount)
                })
                subtotal -= abs(disc_amount)
                
            # Calculate tax
            from routers.settings import get_setting
            try:
                tax_rate = float(get_setting("tax_percent", "10")) / 100.0
            except (ValueError, TypeError):
                tax_rate = 0.10
                
            tax_amount = round(subtotal * tax_rate, 2)
            booking_total = round(subtotal + tax_amount, 2)
            
            # Validate: advance payment cannot exceed total booking amount (including tax)
            if advance_amount > booking_total:
                from urllib.parse import quote
                error_msg = quote(f"Advance payment ({advance_amount}) cannot exceed total booking amount ({booking_total})")
                return RedirectResponse(f"/bookings/create?error={error_msg}", status_code=303)

            db.execute("""
                INSERT INTO bookings (
                    customer_id, room_number, check_in, expected_check_out,
                    room_rate, num_nights, extra_charges, total_amount,
                    status, created_at, updated_at,
                    advance_amount, amount_paid, payment_status,
                    pax, reference_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                customer_id, room_number, check_in, expected_out,
                rate, nights, json.dumps(initial_charges), booking_total,
                booking_status,  # 'Reserved' for reservations, 'Booked' for immediate check-in
                now.isoformat(), now.isoformat(),
                advance_amount, advance_amount, 
                "Partial" if advance_amount > 0 else "Unpaid",
                pax, reference_id
            ))

            # Record Advance Payment if any
            if advance_amount > 0:
                booking_id = db.execute("SELECT last_insert_rowid() as id").fetchone()['id']
                
                db.execute("""
                    INSERT INTO payments (booking_id, amount, payment_method, payment_type, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (booking_id, advance_amount, payment_method, 'advance', 'Initial Deposit', now.isoformat()))
                
                _create_financial_transaction(
                    db, 'income', advance_amount, 'Bookings',
                    f"Advance Payment (Booking #{booking_id})", booking_id
                )

            # 4) Mark room as occupied ONLY for immediate check-ins (not reservations)
            if booking_status == 'Booked':
                _mark_room_status(db, room_number, status="Occupied")

    except Exception as e:
        # Redirect back to create page with error message
        from urllib.parse import quote
        error_msg = quote(f"Check-in failed: {e}")
        return RedirectResponse(f"/bookings/create?error={error_msg}", status_code=303)

    return RedirectResponse("/bookings", status_code=303)


@router.post("/activate_reservation/{booking_id}")
def activate_reservation(booking_id: int):
    """Convert a Reserved booking to Active check-in.
    Check-in date becomes NOW, expected_check_out stays at original date.
    If check-in is late (after reserved date), num_nights is shortened and
    an optional late check-in penalty fee is applied.
    """
    now = datetime.now()
    
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        
        if not b:
            return RedirectResponse("/bookings", status_code=303)
        
        if b["status"] != "Reserved":
            return RedirectResponse("/bookings", status_code=303)
        
        room_number = b["room_number"]
        
        # Prevent early check-in if room is currently occupied by someone else
        db.execute("SELECT status FROM rooms WHERE room_number = ?", (room_number,))
        room = db.fetchone()
        if room and room["status"] in ("Occupied", "Maintenance", "Out of Order"):
            from urllib.parse import quote
            err = quote(f"Cannot check in: Room {room_number} is currently {room['status']}.")
            return RedirectResponse(f"/bookings?error={err}", status_code=303)

        original_check_in = _dt_parse(b["check_in"])
        original_checkout = _dt_parse(b["expected_check_out"])
        original_nights = int(b["num_nights"])
        room_rate = float(b["room_rate"])
        
        # Keep original expected_check_out — guest booked UNTIL that date
        # Recalculate num_nights based on actual check-in to original checkout
        if original_checkout:
            new_nights = max(1, (original_checkout.date() - now.date()).days)
        else:
            new_nights = original_nights
        
        # Parse existing extra charges
        existing_charges = []
        if b["extra_charges"]:
            try:
                existing_charges = json.loads(b["extra_charges"])
                if not isinstance(existing_charges, list):
                    existing_charges = []
            except:
                existing_charges = []
        
        # Apply late check-in penalty if checking in after reserved date
        late_days = 0
        if original_check_in and now.date() > original_check_in.date():
            late_days = (now.date() - original_check_in.date()).days
            from routers.settings import get_setting
            penalty_pct = float(get_setting("late_checkin_penalty_percent", "0"))
            if penalty_pct > 0:
                # Penalty based on the missed nights × room rate × penalty %
                penalty_amount = round(late_days * room_rate * (penalty_pct / 100), 2)
                if penalty_amount > 0:
                    existing_charges.append({
                        "description": f"Late check-in penalty ({late_days} day(s) late, {penalty_pct}%)",
                        "amount": penalty_amount
                    })
        
        # Recalculate total
        new_total = _recompute_total(room_rate, new_nights, json.dumps(existing_charges))
        
        db.execute("""
            UPDATE bookings 
            SET status = 'Booked', check_in = ?, num_nights = ?,
                total_amount = ?, extra_charges = ?, updated_at = ?
            WHERE id = ?
        """, (now.isoformat(), new_nights, new_total,
              json.dumps(existing_charges), now.isoformat(), booking_id))
        
        _mark_room_status(db, room_number, status="Occupied")
    
    return RedirectResponse("/bookings", status_code=303)


@router.post("/cancel/{booking_id}")
def cancel_booking(
    booking_id: int,
    refund_type: str = Form("none"),
    refund_percentage: float = Form(50.0)
):
    """Cancel a booking with optional refund processing"""
    from datetime import datetime
    import json
    print(f"[CANCEL] Cancel called for booking {booking_id}, refund_type={refund_type}, pct={refund_percentage}")
    now = datetime.now()
    
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        
        if not b:
            return RedirectResponse("/bookings", status_code=303)
            
        # If already checked out or cancelled, do nothing
        if b["status"] in ("Checked-out", "Cancelled"):
             return RedirectResponse(f"/bookings/{booking_id}/invoice", status_code=303)
        
        # Process refund if there was an advance payment
        amount_paid = float(b["amount_paid"] or 0)
        refund_amount = 0.0
        
        if amount_paid > 0:
            if refund_type == "full":
                refund_amount = amount_paid
            elif refund_type == "partial":
                # Clamp percentage between 1-99
                pct = max(1, min(99, refund_percentage))
                refund_amount = round(amount_paid * pct / 100, 2)
            # else: refund_type == "none", refund_amount stays 0
        
        # Add refund as a negative charge for audit trail
        extra_charges = []
        try:
            if b["extra_charges"]:
                extra_charges = json.loads(b["extra_charges"])
        except:
            pass
        
        if refund_amount > 0:
            extra_charges.append({
                "desc": f"Cancellation Refund ({refund_type.title()})",
                "amount": -refund_amount,
                "date": now.strftime("%Y-%m-%d %H:%M")
            })
        
        # Update booking status
        db.execute("""
            UPDATE bookings 
            SET status = 'Cancelled', 
                updated_at = ?,
                extra_charges = ?
            WHERE id = ?
        """, (now.isoformat(), json.dumps(extra_charges), booking_id))
        
        # Record refund as a financial expense transaction
        if refund_amount > 0:
            db.execute("""
                INSERT INTO financial_transactions (type, amount, category, description, date)
                VALUES ('expense', ?, 'Refund', ?, ?)
            """, (refund_amount, f"Refund for cancelled booking #{booking_id}", now.isoformat()))
        
        # If the room was marked Occupied (for Booked status), release it
        if b["status"] == "Booked":
             _mark_room_status(db, b["room_number"], "Available")

    return RedirectResponse("/bookings", status_code=303)


@router.post("/update_dates")
def update_booking_dates(
    booking_id: int = Form(...),
    check_in_date: str = Form(...),
    nights: int = Form(...)
):
    """
    Update booking dates for Reserved or Booked bookings only.
    Does NOT allow changes once checked in (use extend instead).
    Performs conflict detection before updating.
    """
    from routers.settings import get_setting
    now = datetime.now()
    
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        
        if not b:
            return JSONResponse({"success": False, "error": "Booking not found"}, status_code=404)
        
        # Only allow date changes for Reserved or Booked (not checked in yet)
        if b["status"] not in ("Reserved", "Booked"):
            return JSONResponse({
                "success": False, 
                "error": f"Cannot edit dates for {b['status']} bookings. Use 'Extend Stay' for checked-in guests."
            }, status_code=400)
        
        # For 'Booked' status, check if they're actually checked in
        # (i.e., room is marked as Occupied for this booking)
        # Actually, 'Booked' means actively checked in, so we should only allow Reserved
        # Let me reconsider: Reserved = future booking, Booked = currently staying
        # So we should only allow editing for Reserved status
        if b["status"] == "Booked":
            return JSONResponse({
                "success": False, 
                "error": "Guest is already checked in. Use 'Extend Stay' to modify duration."
            }, status_code=400)
        
        room_number = b["room_number"]
        nights = max(1, int(nights))
        
        # Parse new check-in date
        try:
            check_in_dt = datetime.fromisoformat(check_in_date)
        except:
            try:
                check_in_dt = datetime.strptime(check_in_date, '%Y-%m-%d')
            except:
                try:
                    check_in_dt = datetime.strptime(check_in_date, '%d/%m/%Y')
                except:
                    return JSONResponse({"success": False, "error": "Invalid date format. Please use the date picker."}, status_code=400)
        
        # Calculate checkout date using hotel checkout time
        checkout_time_str = get_setting("checkout_time", "11:00")
        try:
            ch_hour, ch_min = map(int, checkout_time_str.split(':'))
        except:
            ch_hour, ch_min = 11, 0
        
        checkout_date = check_in_dt + timedelta(days=nights)
        expected_out_dt = checkout_date.replace(hour=ch_hour, minute=ch_min, second=0, microsecond=0)
        
        check_in = check_in_dt.isoformat()
        expected_out = expected_out_dt.isoformat()
        
        # Check for conflicts (exclude current booking)
        availability = check_room_availability(db, room_number, check_in, expected_out, exclude_booking_id=booking_id)
        if not availability["available"]:
            return JSONResponse({
                "success": False, 
                "error": availability["message"]
            }, status_code=400)
        
        # Recalculate total with new nights
        new_total = _recompute_total(float(b["room_rate"]), nights, b["extra_charges"])
        
        # Update booking
        db.execute("""
            UPDATE bookings 
            SET check_in = ?, expected_check_out = ?, num_nights = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (check_in, expected_out, nights, new_total, now.isoformat(), booking_id))
        
    return JSONResponse({
        "success": True, 
        "message": f"Booking dates updated: Check-in {check_in_dt.strftime('%b %d')}, {nights} nights",
        "check_in": check_in,
        "expected_check_out": expected_out,
        "nights": nights,
        "total": new_total
    })


@router.post("/change_room")
def change_room(
    booking_id: int = Form(...),
    new_room_number: str = Form(...),
):
    """
    Change the room for a Reserved or Booked booking.
    - Reserved: recalculate entire stay at new room rate
    - Booked (active): split billing — old rate for days stayed, new rate for remaining days
    Creates audit trail in extra_charges and financial transaction for the difference.
    """
    now = datetime.now()
    new_room_number = new_room_number.strip()

    with get_db() as db:
        # 1) Fetch booking
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return JSONResponse({"success": False, "error": "Booking not found"}, status_code=404)

        if b["status"] not in ("Reserved", "Booked"):
            return JSONResponse({
                "success": False,
                "error": f"Cannot change room for {b['status']} bookings."
            }, status_code=400)

        old_room = b["room_number"]
        if new_room_number == old_room:
            return JSONResponse({"success": False, "error": "Same room selected. Choose a different room."}, status_code=400)

        # 2) Validate new room
        db.execute("SELECT price_per_night, status FROM rooms WHERE room_number = ?", (new_room_number,))
        new_room = db.fetchone()
        if not new_room:
            return JSONResponse({"success": False, "error": f"Room {new_room_number} not found."}, status_code=404)

        new_room_status = new_room["status"]
        if new_room_status not in ("Available",):
            return JSONResponse({
                "success": False,
                "error": f"Room {new_room_number} is {new_room_status.lower()} and cannot be assigned."
            }, status_code=400)

        # 3) Check availability for the booking date range
        availability = check_room_availability(
            db, new_room_number, b["check_in"], b["expected_check_out"],
            exclude_booking_id=booking_id
        )
        if not availability["available"]:
            return JSONResponse({"success": False, "error": availability["message"]}, status_code=400)

        new_rate = float(new_room["price_per_night"])
        old_rate = float(b["room_rate"])
        num_nights = int(b["num_nights"])

        # 4) Calculate billing
        try:
            charges = json.loads(b["extra_charges"] or "[]")
        except:
            charges = []

        if b["status"] == "Reserved":
            # Not checked in yet — recalculate entire stay at new rate
            rate_for_booking = new_rate
            audit_desc = f"Room Change: {old_room} → {new_room_number} ({num_nights}N @ ${new_rate:.2f}/night)"
            rate_diff_total = (new_rate - old_rate) * num_nights
        else:
            # Active (Booked) — split billing
            check_in_dt = _dt_parse(b["check_in"])
            if not check_in_dt:
                return JSONResponse({"success": False, "error": "Invalid check-in date on booking."}, status_code=400)

            days_stayed = max(0, (now.date() - check_in_dt.date()).days)

            if days_stayed == 0:
                # Same-day change: charge entirely at new rate
                rate_for_booking = new_rate
                audit_desc = f"Room Change (same day): {old_room} → {new_room_number} ({num_nights}N @ ${new_rate:.2f}/night)"
                rate_diff_total = (new_rate - old_rate) * num_nights
            else:
                # Split billing
                days_remaining = max(0, num_nights - days_stayed)
                old_portion = days_stayed * old_rate
                new_portion = days_remaining * new_rate
                # We'll set room_rate to the new rate for display purposes,
                # and add a billing adjustment charge to reconcile
                rate_for_booking = new_rate
                # The base room total at new rate would be new_rate * num_nights
                # But actual billing is old_portion + new_portion
                # So we need an adjustment: actual - (new_rate * num_nights)
                actual_room_total = old_portion + new_portion
                base_at_new_rate = new_rate * num_nights
                adjustment = actual_room_total - base_at_new_rate

                rate_diff_total = actual_room_total - (old_rate * num_nights)

                if abs(adjustment) > 0.01:
                    charges.append({
                        "description": f"Split billing adj: {days_stayed}N @ ${old_rate:.2f} + {days_remaining}N @ ${new_rate:.2f}",
                        "amount": round(adjustment, 2)
                    })

                audit_desc = f"Room Change: {old_room} → {new_room_number} ({days_stayed}N old @ ${old_rate:.2f} + {days_remaining}N new @ ${new_rate:.2f})"

        # Add audit trail charge (description only, $0 amount) for tracking
        charges.append({
            "description": f"🔄 {audit_desc}",
            "amount": 0
        })

        # 5) Recalculate total
        new_total = _recompute_total(rate_for_booking, num_nights, json.dumps(charges))

        # 6) Update booking
        db.execute("""
            UPDATE bookings
            SET room_number = ?, room_rate = ?, extra_charges = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (new_room_number, rate_for_booking, json.dumps(charges), new_total, now.isoformat(), booking_id))

        # 7) Update room statuses
        if b["status"] == "Booked":
            # Active booking — release old room, occupy new room
            _mark_room_status(db, old_room, status="Available", housekeeping="Dirty")
            _mark_room_status(db, new_room_number, status="Occupied")

        # For Reserved, room statuses don't change (rooms aren't occupied for reservations)

        # 8) Audit trail only — room changes are rate adjustments, NOT cash transactions.
        #    The actual financial settlement (refund or additional charge) happens at checkout.
        _create_financial_transaction(
            db, 'info', 0, 'Operations',
            f"Room Change {old_room}→{new_room_number}: rate ${old_rate:.2f}→${new_rate:.2f}/night, "
            f"total ${float(b['total_amount']):.2f}→${new_total:.2f} (Booking #{booking_id})", booking_id
        )

    return JSONResponse({
        "success": True,
        "message": f"Room changed from {old_room} to {new_room_number}",
        "old_room": old_room,
        "new_room": new_room_number,
        "old_rate": old_rate,
        "new_rate": new_rate,
        "new_total": new_total,
        "audit": audit_desc
    })

@router.post("/add_charge")
def add_charge(
    booking_id: int = Form(...),
    description: str = Form(...),
    amount: float = Form(...)
):
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return RedirectResponse("/bookings", status_code=303)

        charges = []
        if b["extra_charges"]:
            try:
                charges = json.loads(b["extra_charges"]) or []
            except Exception:
                charges = []
        charges.append({"description": description.strip(), "amount": float(amount)})

        new_total = _recompute_total(float(b["room_rate"]), int(b["num_nights"]), json.dumps(charges))
        now = datetime.now().isoformat()
        db.execute("""
            UPDATE bookings
            SET extra_charges = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (json.dumps(charges), new_total, now, booking_id))

    return RedirectResponse("/bookings", status_code=303)

@router.post("/extend")
def extend_stay(
    booking_id: int = Form(...),
    extra_nights: int = Form(...)
):
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return RedirectResponse("/bookings", status_code=303)

        extra_nights = max(1, int(extra_nights))
        ci = _dt_parse(b["check_in"]) or datetime.now()
        new_nights = int(b["num_nights"]) + extra_nights
        new_expected_dt = ci + timedelta(days=new_nights)
        new_expected = new_expected_dt.isoformat()

        # Check if room is available for the extended period
        current_checkout = _dt_parse(b["expected_check_out"]) or ci + timedelta(days=int(b["num_nights"]))
        availability = check_room_availability(
            db, b["room_number"],
            current_checkout.isoformat(),  # Only check from current checkout onward
            new_expected,
            exclude_booking_id=booking_id
        )
        
        if not availability["available"]:
            # Return JSON with conflict info — allows the frontend to show inline message
            # and offer room change option
            # Get available rooms for the extended period
            db.execute("""
                SELECT room_number, room_type, price_per_night, status
                FROM rooms
                WHERE room_number != ? AND status != 'Out of Service'
                ORDER BY CAST(room_number AS INTEGER), room_number
            """, (b["room_number"],))
            alt_rooms = []
            for r in db.fetchall():
                alt_avail = check_room_availability(
                    db, r["room_number"],
                    ci.isoformat(), new_expected, exclude_booking_id=booking_id
                )
                if alt_avail["available"]:
                    alt_rooms.append({
                        "room_number": r["room_number"],
                        "room_type": r["room_type"],
                        "price_per_night": float(r["price_per_night"]),
                    })
            
            return JSONResponse({
                "success": False,
                "conflict": True,
                "message": availability["message"],
                "available_rooms": alt_rooms,
                "booking_id": booking_id,
                "extra_nights": extra_nights
            }, status_code=409)

        new_total = _recompute_total(float(b["room_rate"]), new_nights, b["extra_charges"])

        now = datetime.now().isoformat()
        db.execute("""
            UPDATE bookings
            SET num_nights = ?, expected_check_out = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (new_nights, new_expected, new_total, now, booking_id))
        
        # Audit trail only — extensions increase the amount OWED, not cash received.
        # The actual payment is recorded at checkout.
        extension_fee = float(b["room_rate"]) * extra_nights
        _create_financial_transaction(
            db, 'info', 0, 'Operations',
            f"Stay Extended +{extra_nights}N: total ${float(b['total_amount']):.2f}→${new_total:.2f} (Booking #{booking_id})", booking_id
        )

    return RedirectResponse("/bookings", status_code=303)






@router.post("/late_checkout")
def late_checkout_fee(
    booking_id: int = Form(...),
    hours: int = Form(...),
    hourly_rate: float = Form(...)
):
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return RedirectResponse("/bookings", status_code=303)

        hours = max(1, int(hours))
        fee = float(hourly_rate) * hours

        charges = []
        if b["extra_charges"]:
            try:
                charges = json.loads(b["extra_charges"]) or []
            except Exception:
                charges = []
        charges.append({"description": f"Late checkout ({hours}h)", "amount": fee})

        new_total = _recompute_total(float(b["room_rate"]), int(b["num_nights"]), json.dumps(charges))

        now = datetime.now().isoformat()
        db.execute("""
            UPDATE bookings
            SET extra_charges = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (json.dumps(charges), new_total, now, booking_id))

    return RedirectResponse("/bookings", status_code=303)

@router.post("/checkout")
def checkout(booking_id: int = Form(...), payment_method: str = Form("")):
    now_dt = datetime.now()
    now_str = now_dt.isoformat()
    
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return RedirectResponse("/bookings", status_code=303)
        
        # CRITICAL: Prevent double checkout
        if b["status"] == "Checked-out":
            return RedirectResponse(f"/bookings/{booking_id}/invoice", status_code=303)

        # Parse extra_charges
        try:
            charges = json.loads(b["extra_charges"] or "[]")
        except:
            charges = []

        room_rate = float(b["room_rate"])
        check_in_dt = _dt_parse(b["check_in"])
        expected_out = _dt_parse(b["expected_check_out"])
        booked_nights = int(b["num_nights"])
        
        # ── Calculate ACTUAL nights stayed, capped at booked nights (min 1) ──
        if check_in_dt:
            raw_days = max(1, (now_dt.date() - check_in_dt.date()).days)
            actual_nights = min(booked_nights, raw_days)
        else:
            actual_nights = booked_nights
        
        # ── Late Checkout Fee (if > 2 hours past expected checkout) ──
        if expected_out and (now_dt - expected_out).total_seconds() > 7200:
            late_hours = int((now_dt - expected_out).total_seconds() / 3600)
            fee = min(late_hours * (room_rate / 24) * 1.5, room_rate)
            charges.append({
                "description": f"Late Checkout Fee ({late_hours}h)",
                "amount": round(fee, 2)
            })
        
        # ── Calculate actual charges (room + extras) ──
        actual_room_charge = room_rate * actual_nights
        extras_total = sum(float(c.get("amount", 0)) for c in charges)
        actual_subtotal = round(actual_room_charge + extras_total, 2)
        
        # ── Calculate Tax ──
        from routers.settings import get_setting
        tax_pct_str = get_setting("tax_percent", "10")
        try:
            tax_rate = float(tax_pct_str) / 100.0
        except (ValueError, TypeError):
            tax_rate = 0.10
        
        tax_amount = round(actual_subtotal * tax_rate, 2)
        grand_total = round(actual_subtotal + tax_amount, 2)
        
        # ── Handle advance payment ──
        try:
            advance_amount = float(b["advance_amount"] or 0)
        except (KeyError, TypeError):
            advance_amount = 0.0
        
        # ── Determine balance: positive = guest owes, negative = refund due ──
        remaining_balance = round(grand_total - advance_amount, 2)
        
        # Add informational refund line if advance exceeded actual charges + tax
        # (this is for display only — does NOT change calculations)
        if remaining_balance < 0:
            refund_amount = abs(remaining_balance)
            charges.append({
                "description": f"Refund (advance exceeded charges)",
                "amount": -refund_amount
            })
        
        # ── Update booking ──
        # Store grand_total as total_amount
        db.execute("""
            UPDATE bookings
            SET actual_check_out = ?, extra_charges = ?, total_amount = ?,
                num_nights = ?, status = 'Checked-out', updated_at = ?
            WHERE id = ?
        """, (now_str, json.dumps(charges), grand_total, actual_nights, now_str, booking_id))
        
        # ── Update amount_paid and payment_status ──
        if remaining_balance > 0:
            # Guest pays the remaining balance
            new_amount_paid = advance_amount + remaining_balance
            db.execute("UPDATE bookings SET amount_paid = ?, payment_status = 'Paid' WHERE id = ?",
                       (new_amount_paid, booking_id))
        elif remaining_balance == 0:
            db.execute("UPDATE bookings SET payment_status = 'Paid' WHERE id = ?", (booking_id,))
        else:
            # Refund case — mark as refunded
            db.execute("UPDATE bookings SET payment_status = 'Refunded' WHERE id = ?", (booking_id,))
        
        # ── Financial transactions ──
        if remaining_balance > 0:
            # Guest owes money — record checkout payment
            pm = payment_method or "Cash"
            db.execute("""
                INSERT INTO payments (booking_id, amount, payment_method, payment_type, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (booking_id, remaining_balance, pm, 'checkout', 'Checkout balance payment', now_str))
            
            _create_financial_transaction(
                db, 'income', remaining_balance, 'Bookings',
                f"Checkout Balance (Booking #{booking_id}, {actual_nights} nights)", booking_id
            )
        elif remaining_balance < 0:
            # Hotel owes guest a refund (could be early checkout, room downgrade, or overpayment)
            refund = abs(remaining_balance)
            if actual_nights < booked_nights:
                refund_reason = f"Early Checkout Refund ({actual_nights} of {booked_nights} nights)"
            else:
                refund_reason = f"Overpayment Refund (advance ${advance_amount:.2f} exceeded total ${grand_total:.2f})"
            _create_financial_transaction(
                db, 'expense', refund, 'Refunds',
                f"{refund_reason} (Booking #{booking_id})", booking_id
            )

        _mark_room_status(db, b["room_number"], status="Available", housekeeping="Dirty")

    return RedirectResponse(f"/bookings/{booking_id}/invoice", status_code=303)


# ==================== INVOICE EDITING ENDPOINTS ====================

@router.post("/update/{booking_id}")
def update_booking(
    booking_id: int,
    room_rate: float = Form(None),
    num_nights: int = Form(None),
    expected_check_out: str = Form(None),
    customer_name: str = Form(None),
    customer_email: str = Form(None),
    customer_phone: str = Form(None),
):
    """Update booking details (room rate, nights, dates, customer info)."""
    now = datetime.now().isoformat()
    
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return JSONResponse({"success": False, "error": "Booking not found"}, status_code=404)
        
        # Update room rate and/or nights if provided
        update_booking_fields = []
        update_booking_params = []
        
        if room_rate is not None:
            update_booking_fields.append("room_rate = ?")
            update_booking_params.append(room_rate)
        
        if num_nights is not None:
            update_booking_fields.append("num_nights = ?")
            update_booking_params.append(num_nights)
        
        if expected_check_out:
            update_booking_fields.append("expected_check_out = ?")
            update_booking_params.append(expected_check_out)
        
        if update_booking_fields:
            # Recalculate total
            new_rate = room_rate if room_rate is not None else float(b["room_rate"])
            new_nights = num_nights if num_nights is not None else int(b["num_nights"])
            new_total = _recompute_total(new_rate, new_nights, b["extra_charges"])
            
            update_booking_fields.append("total_amount = ?")
            update_booking_params.append(new_total)
            update_booking_fields.append("updated_at = ?")
            update_booking_params.append(now)
            update_booking_params.append(booking_id)
            
            db.execute(f"""
                UPDATE bookings SET {', '.join(update_booking_fields)} WHERE id = ?
            """, update_booking_params)
        
        # Update customer info if provided
        if any([customer_name, customer_email, customer_phone]):
            customer_id = b["customer_id"]
            update_customer_fields = []
            update_customer_params = []
            
            if customer_name:
                update_customer_fields.append("name = ?")
                update_customer_params.append(customer_name)
            if customer_email:
                update_customer_fields.append("email = ?")
                update_customer_params.append(customer_email)
            if customer_phone:
                update_customer_fields.append("phone = ?")
                update_customer_params.append(customer_phone)
            
            if update_customer_fields:
                update_customer_fields.append("updated_at = ?")
                update_customer_params.append(now)
                update_customer_params.append(customer_id)
                db.execute(f"""
                    UPDATE customers SET {', '.join(update_customer_fields)} WHERE id = ?
                """, update_customer_params)
    
    return RedirectResponse(f"/bookings/{booking_id}/invoice", status_code=303)


@router.post("/add_charge/{booking_id}")
def add_charge(
    booking_id: int,
    description: str = Form(...),
    amount: float = Form(...),
):
    """Add an extra charge or credit (use negative amount for discount/credit)."""
    now = datetime.now().isoformat()
    
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return JSONResponse({"success": False, "error": "Booking not found"}, status_code=404)
        
        # Parse existing charges
        try:
            charges = json.loads(b["extra_charges"] or "[]")
        except:
            charges = []
        
        # Add new charge
        charges.append({
            "description": description,
            "amount": amount
        })
        
        # Recalculate total
        new_total = _recompute_total(float(b["room_rate"]), int(b["num_nights"]), json.dumps(charges))
        
        db.execute("""
            UPDATE bookings SET extra_charges = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (json.dumps(charges), new_total, now, booking_id))
    
    return RedirectResponse(f"/bookings/{booking_id}/invoice", status_code=303)


@router.post("/remove_charge/{booking_id}")
def remove_charge(
    booking_id: int,
    charge_index: int = Form(...),
):
    """Remove an extra charge by its index."""
    now = datetime.now().isoformat()
    
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return JSONResponse({"success": False, "error": "Booking not found"}, status_code=404)
        
        try:
            charges = json.loads(b["extra_charges"] or "[]")
        except:
            charges = []
        
        # Remove charge at index
        if 0 <= charge_index < len(charges):
            charges.pop(charge_index)
        
        # Recalculate total
        new_total = _recompute_total(float(b["room_rate"]), int(b["num_nights"]), json.dumps(charges))
        
        db.execute("""
            UPDATE bookings SET extra_charges = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (json.dumps(charges), new_total, now, booking_id))
    
    return RedirectResponse(f"/bookings/{booking_id}/invoice", status_code=303)


@router.post("/apply_discount/{booking_id}")
def apply_discount(
    booking_id: int,
    discount_type: str = Form(...),  # 'percentage' or 'fixed'
    discount_value: float = Form(...),
    reason: str = Form("Discount"),
):
    """Apply a discount (percentage or fixed amount) to a booking."""
    now = datetime.now().isoformat()
    
    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return JSONResponse({"success": False, "error": "Booking not found"}, status_code=404)
        
        try:
            charges = json.loads(b["extra_charges"] or "[]")
        except:
            charges = []
        
        # Calculate discount amount
        room_total = float(b["room_rate"]) * int(b["num_nights"])
        
        if discount_type == "percentage":
            discount_amount = room_total * (discount_value / 100)
            description = f"{reason} ({discount_value}%)"
        else:
            discount_amount = discount_value
            description = f"{reason}"
        
        # Add discount as negative charge
        charges.append({
            "description": f"💸 {description}",
            "amount": -abs(discount_amount)  # Ensure it's negative
        })
        
        # Recalculate total
        new_total = _recompute_total(float(b["room_rate"]), int(b["num_nights"]), json.dumps(charges))
        
        db.execute("""
            UPDATE bookings SET extra_charges = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (json.dumps(charges), new_total, now, booking_id))
    
    return RedirectResponse(f"/bookings/{booking_id}/invoice", status_code=303)


from fastapi import Request
from starlette.responses import HTMLResponse, Response
from datetime import datetime
import json

def _build_invoice_context(request: Request, booking_id: int):
    # reuse your DB helper exactly as in invoice_view
    from dependencies import get_db

    with get_db() as db:
        db.execute("""
            SELECT
                b.*,
                c.name  AS customer_name,
                c.email AS customer_email,
                c.phone AS customer_phone,
                ref.name AS reference_name
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            LEFT JOIN customers ref ON ref.id = b.reference_id
            WHERE b.id = ?
        """, (booking_id,))
        row = db.fetchone()

    if not row:
        return None  # caller decides 404

    try:
        charges = json.loads(row["extra_charges"] or "[]")
    except Exception:
        charges = []

    room_line_amount = float(row["room_rate"]) * int(row["num_nights"])
    # All extra charges (including negative discounts/refunds) affect the subtotal
    extras_total = sum(float(x.get("amount", 0)) for x in charges)
    subtotal = round(room_line_amount + extras_total, 2)
    # Read tax rate from settings (default 10%)
    from routers.settings import get_setting
    tax_pct_str = get_setting("tax_percent", "10")
    try:
        tax_rate = float(tax_pct_str) / 100.0
    except (ValueError, TypeError):
        tax_rate = 0.10
    tax = round(subtotal * tax_rate, 2)
    grand_total = round(subtotal + tax, 2)
    
    amount_paid = float(row["amount_paid"] or 0)
    advance_amount = float(row["advance_amount"] or 0)
    
    # Calculate balance or refund
    # Use max(amount_paid, advance_amount) for safety — advance IS a payment
    effective_paid = max(amount_paid, advance_amount)
    balance_due = round(max(0, grand_total - effective_paid), 2)
    refund_due = round(max(0, advance_amount - grand_total), 2) if row["status"] == "Checked-out" else 0.0

    return {
        "request": request,
        "b": row,
        "room_line": {
            "description": f"Room {row['room_number']} × {row['num_nights']} night(s)",
            "amount": room_line_amount,
        },
        "charges": charges,
        "subtotal": subtotal,
        "tax": tax,
        "grand_total": grand_total,
        "amount_paid": amount_paid,
        "advance_amount": advance_amount,
        "balance_due": balance_due,
        "refund_due": refund_due,
        "tax_rate_pct": int(tax_rate * 100),
        "generated_at": datetime.now(),
        "hotel": {
            "name": get_setting("hotel_name", "HavenDesk"),
            "address": get_setting("hotel_address", "123 Example Rd, City"),
            "email": get_setting("hotel_email", "billing@havendesk.example"),
            "phone": get_setting("hotel_phone", "+880 1XXX-XXXXXX"),
        },
        "currency": get_setting("currency_symbol", "$"),
    }


# --- Invoice (HTML) ---
@router.get("/{booking_id}/invoice", response_class=HTMLResponse)
def invoice_view(booking_id: int, request: Request):
    ctx = _build_invoice_context(request, booking_id)
    if not ctx:
        return HTMLResponse("Invoice not found", status_code=404)
    return templates.TemplateResponse("invoice.html", ctx)

@router.get("/{booking_id}/invoice.pdf")
def invoice_pdf(booking_id: int, request: Request):
    from weasyprint import HTML

    ctx = _build_invoice_context(request, booking_id)
    if not ctx:
        return Response("Invoice not found", status_code=404)

    # Render the SAME template so HTML & PDF stay identical
    html = templates.get_template("invoice.html").render(ctx)

    # base_url lets WeasyPrint resolve absolute/relative assets if you add any
    pdf_bytes = HTML(string=html, base_url=str(request.base_url)).write_pdf()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="invoice-{booking_id}.pdf"'}
    )


# ---------- Payment Recording ----------
@router.post("/payment")
def record_payment(
    booking_id: int = Form(...),
    amount: float = Form(...),
    payment_method: str = Form("Cash"),
    payment_type: str = Form("partial"),  # 'advance', 'partial', 'final'
    notes: str = Form("")
):
    """
    Record a payment for a booking.
    Updates booking.amount_paid and creates payment history record.
    """
    now = datetime.now().isoformat()
    
    with get_db() as db:
        # Get current booking
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        b = db.fetchone()
        if not b:
            return RedirectResponse("/bookings", status_code=303)
        
        # Calculate new amount_paid
        current_paid = float(b["amount_paid"] or 0)
        current_advance = float(b["advance_amount"] or 0)
        
        if payment_type == "advance":
            new_advance = current_advance + float(amount)
            new_paid = current_paid + float(amount)  # advance IS a payment
            db.execute("""
                UPDATE bookings
                SET advance_amount = ?, amount_paid = ?, updated_at = ?
                WHERE id = ?
            """, (new_advance, new_paid, now, booking_id))
            total_paid = new_paid
        else:
            new_paid = current_paid + float(amount)
            db.execute("""
                UPDATE bookings
                SET amount_paid = ?, updated_at = ?
                WHERE id = ?
            """, (new_paid, now, booking_id))
            total_paid = new_paid
        
        # Calculate payment status
        total_due = float(b["total_amount"] or 0)
        if total_paid >= total_due:
            payment_status = "Paid"
        elif total_paid > 0:
            payment_status = "Partial"
        else:
            payment_status = "Unpaid"
        
        db.execute("UPDATE bookings SET payment_status = ? WHERE id = ?", (payment_status, booking_id))
        
        # Record in payments table
        db.execute("""
            INSERT INTO payments (booking_id, amount, payment_method, payment_type, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (booking_id, float(amount), payment_method, payment_type, notes.strip() or None, now))
        
        # Create financial transaction
        _create_financial_transaction(
            db, 'income', float(amount), 'Payments',
            f"Payment ({payment_type.title()}) - Booking #{booking_id}", booking_id
        )

    return RedirectResponse("/bookings", status_code=303)


@router.get("/{booking_id}/payments")
def get_payment_history(booking_id: int):
    """Get all payments for a booking."""
    with get_db() as db:
        db.execute("""
            SELECT id, amount, payment_method, payment_type, notes, created_at
            FROM payments WHERE booking_id = ? ORDER BY created_at DESC
        """, (booking_id,))
        rows = db.fetchall()
        
        payments = [{
            "id": r["id"], "amount": float(r["amount"]),
            "method": r["payment_method"], "type": r["payment_type"],
            "notes": r["notes"], "date": r["created_at"]
        } for r in rows]
        
        db.execute("""
            SELECT total_amount, amount_paid, advance_amount, payment_status
            FROM bookings WHERE id = ?
        """, (booking_id,))
        b = db.fetchone()
        
        if not b:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        
        total = float(b["total_amount"] or 0)
        paid = float(b["amount_paid"] or 0)
        advance = float(b["advance_amount"] or 0)
        balance_due = max(0, total - paid - advance)

    return JSONResponse({
        "booking_id": booking_id,
        "payments": payments,
        "total_amount": total,
        "amount_paid": paid,
        "advance_amount": advance,
        "balance_due": round(balance_due, 2),
        "payment_status": b["payment_status"]
    })


# ==================== MULTI-ROOM BOOKING GROUP ENDPOINTS ====================

@router.get("/create", response_class=HTMLResponse)
def booking_create_page(request: Request, error: str = ""):
    return templates.TemplateResponse("bookings-create.html", {"request": request, "error": error, "currency": get_setting("currency_symbol", "$")})



@router.post("/checkin-group", response_class=HTMLResponse)
def checkin_group(
    request: Request,
    # Customer info
    email: str = Form(None),
    phone: str = Form(None),
    customer_name: str = Form(None),
    id_proof: str = Form(None),
    address: str = Form(None),
    # Multi-room info (parallel arrays)
    room_numbers: list[str] = Form(...),
    room_nights: list[int] = Form(...),
    # Payment
    payment_method: str = Form(""),
    advance_amount: float = Form(0.0),
    # Reservation
    is_reservation: bool = Form(False),
    check_in_date: str = Form(None),
    # Group label
    group_label: str = Form(""),
    # Services
    selected_services: list[str] = Form([]),
    room_services_json: str = Form(""),
    # Discount during booking
    discount_type: str = Form(""),
    discount_value: float = Form(0.0),
    discount_reason: str = Form(""),
    # Pax and Reference
    pax: int = Form(1),
    reference_id: int = Form(None),
):
    """Create multiple bookings linked via a booking group."""
    from urllib.parse import quote
    from routers.settings import get_setting

    now = datetime.now()
    advance_amount = float(advance_amount) if advance_amount else 0.0

    try:
        with get_db() as db:
            # 1) Customer lookup / creation (reuse existing helpers)
            existing = _find_customer_by_email_or_phone(db, email, phone)
            if existing:
                _maybe_update_customer_profile(db, existing, customer_name, email, phone, id_proof, address)
                customer_id = existing["id"]
            else:
                if not (customer_name and customer_name.strip()):
                    raise ValueError("Customer name is required for a new customer.")
                customer_id = _create_customer(db,
                    (customer_name or "").strip(),
                    (email or "").strip() or None,
                    (phone or "").strip() or None,
                    (id_proof or "").strip() or None,
                    (address or "").strip() or None)

            if reference_id and int(reference_id) == int(customer_id):
                raise ValueError("A guest cannot set themselves as a reference.")

            # 2) Create the booking group
            label = group_label.strip() or f"{(customer_name or 'Guest').strip()} - {len(room_numbers)} rooms"
            db.execute("""
                INSERT INTO booking_groups (customer_id, label, created_at)
                VALUES (?, ?, ?)
            """, (customer_id, label, now.isoformat()))
            group_id = db.execute("SELECT last_insert_rowid() as id").fetchone()["id"]

            # 3) Checkout time setting
            checkout_time_str = get_setting("checkout_time", "11:00")
            try:
                ch_hour, ch_min = map(int, checkout_time_str.split(':'))
            except:
                ch_hour, ch_min = 11, 0

            # Parse per-room services mapping (if provided)
            per_room_services = {}
            if room_services_json and room_services_json.strip():
                try:
                    per_room_services = json.loads(room_services_json)
                    # per_room_services = {"101": [1, 3], "202": [2]} etc.
                except:
                    per_room_services = {}

            # Pre-fetch all needed service info
            all_svc_ids = set()
            if per_room_services:
                for svc_ids in per_room_services.values():
                    all_svc_ids.update(int(s) for s in svc_ids)
            elif selected_services:
                all_svc_ids.update(int(s) for s in selected_services)

            svc_lookup = {}
            if all_svc_ids:
                placeholders = ','.join('?' for _ in all_svc_ids)
                db.execute(f"SELECT * FROM services_catalog WHERE id IN ({placeholders})", list(all_svc_ids))
                for svc in db.fetchall():
                    svc_lookup[int(svc["id"])] = {"description": svc["name"], "amount": float(svc["price"])}

            # 4) Calculate total group cost for advance payment splitting
            room_data_list = []
            group_total = 0.0

            try:
                tax_rate = float(get_setting("tax_percent", "10")) / 100.0
            except:
                tax_rate = 0.10

            for i, rn in enumerate(room_numbers):
                rn = rn.strip()
                nights = max(1, int(room_nights[i]) if i < len(room_nights) else 1)

                rate, status = _room_rate_and_status(db, rn)
                if status == "OutOfService":
                    raise ValueError(f"Room {rn} is out of service.")

                # Determine check-in/out dates
                if is_reservation and check_in_date:
                    try:
                        check_in_dt = datetime.fromisoformat(check_in_date)
                    except:
                        check_in_dt = datetime.strptime(check_in_date, '%Y-%m-%d')
                    if check_in_dt.date() < now.date():
                        raise ValueError(f"Reservation date cannot be in the past.")
                    check_in = check_in_dt.isoformat()
                    checkout_date = check_in_dt + timedelta(days=nights)
                    booking_status = 'Reserved'
                else:
                    check_in = now.isoformat()
                    check_in_dt = now
                    checkout_date = now + timedelta(days=nights)
                    booking_status = 'Booked'

                    if status == "Occupied":
                        raise ValueError(f"Room {rn} is occupied.")

                expected_out_dt = checkout_date.replace(hour=ch_hour, minute=ch_min, second=0, microsecond=0)
                expected_out = expected_out_dt.isoformat()

                # Availability check
                availability = check_room_availability(db, rn, check_in, expected_out)
                if not availability["available"]:
                    raise ValueError(f"Room {rn}: {availability['message']}")

                # Calculate cost
                subtotal = float(rate) * nights

                # Build per-room service charges
                room_service_charges = []
                if per_room_services:
                    # Per-room assignment
                    room_svc_ids = per_room_services.get(rn, [])
                    for sid in room_svc_ids:
                        svc_info = svc_lookup.get(int(sid))
                        if svc_info:
                            room_service_charges.append(dict(svc_info))
                elif selected_services:
                    # Legacy: all services to all rooms
                    for sid in selected_services:
                        svc_info = svc_lookup.get(int(sid))
                        if svc_info:
                            room_service_charges.append(dict(svc_info))

                svc_total = sum(c["amount"] for c in room_service_charges)

                # Apply discount per-room (proportional for percentage, split evenly for fixed)
                room_disc_charges = []
                if discount_type and discount_value and discount_value > 0:
                    if discount_type == "percentage":
                        room_subtotal_for_disc = float(rate) * nights
                        disc_amount = round(room_subtotal_for_disc * (discount_value / 100), 2)
                        disc_desc = f"Discount ({discount_value}%)"
                    else:
                        # Split fixed discount evenly across rooms
                        disc_amount = round(float(discount_value) / len(room_numbers), 2)
                        disc_desc = f"Discount (Fixed)"
                    if discount_reason:
                        disc_desc += f" - {discount_reason}"
                    room_disc_charges.append({
                        "description": disc_desc,
                        "amount": -abs(disc_amount)
                    })
                    svc_total -= abs(disc_amount)

                all_charges = room_service_charges + room_disc_charges
                taxable = subtotal + svc_total
                tax_amount = round(taxable * tax_rate, 2)
                booking_total = round(taxable + tax_amount, 2)
                group_total += booking_total

                room_data_list.append({
                    "room_number": rn, "nights": nights, "rate": rate,
                    "check_in": check_in, "expected_out": expected_out,
                    "booking_status": booking_status, "booking_total": booking_total,
                    "service_charges": all_charges,
                })

            # 5) Validate advance
            if advance_amount > group_total:
                raise ValueError(f"Advance payment ({advance_amount}) exceeds group total ({group_total})")

            # 6) Split advance proportionally by room cost (fix rounding to match exact input)
            advance_splits = []
            if group_total > 0 and advance_amount > 0:
                running_sum = 0.0
                for i, rd in enumerate(room_data_list):
                    if i < len(room_data_list) - 1:
                        share = round(advance_amount * (rd["booking_total"] / group_total), 2)
                        advance_splits.append(share)
                        running_sum += share
                    else:
                        # Last room gets the remainder to avoid rounding error
                        advance_splits.append(round(advance_amount - running_sum, 2))
            else:
                advance_splits = [0.0] * len(room_data_list)

            booking_ids = []
            for i, rd in enumerate(room_data_list):
                room_advance = advance_splits[i]

                db.execute("""
                    INSERT INTO bookings (
                        customer_id, room_number, check_in, expected_check_out,
                        room_rate, num_nights, extra_charges, total_amount,
                        status, created_at, updated_at,
                        advance_amount, amount_paid, payment_status,
                        group_id, pax, reference_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    customer_id, rd["room_number"], rd["check_in"], rd["expected_out"],
                    rd["rate"], rd["nights"], json.dumps(rd["service_charges"]), rd["booking_total"],
                    rd["booking_status"], now.isoformat(), now.isoformat(),
                    room_advance, room_advance,
                    "Partial" if room_advance > 0 else "Unpaid",
                    group_id, pax, reference_id
                ))
                bid = db.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
                booking_ids.append(bid)

                # Record advance payment
                if room_advance > 0:
                    db.execute("""
                        INSERT INTO payments (booking_id, amount, payment_method, payment_type, notes, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (bid, room_advance, payment_method, 'advance',
                          f'Group #{group_id} advance share', now.isoformat()))
                    _create_financial_transaction(
                        db, 'income', room_advance, 'Bookings',
                        f"Advance Payment (Booking #{bid}, Group #{group_id})", bid
                    )

                # Mark room occupied for immediate check-ins
                if rd["booking_status"] == 'Booked':
                    _mark_room_status(db, rd["room_number"], status="Occupied")

    except Exception as e:
        error_msg = quote(f"Group check-in failed: {e}")
        return RedirectResponse(f"/bookings/create?error={error_msg}", status_code=303)

    return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)


# ---------- Group overview page ----------
@router.get("/group/{group_id}", response_class=HTMLResponse)
def group_overview(group_id: int, request: Request):
    """View all bookings in a group."""
    with get_db() as db:
        # Get group info
        db.execute("SELECT * FROM booking_groups WHERE id = ?", (group_id,))
        group = db.fetchone()
        if not group:
            return HTMLResponse("Group not found", status_code=404)

        # Get all bookings in this group
        db.execute("""
            SELECT b.*, c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            WHERE b.group_id = ?
            ORDER BY b.room_number
        """, (group_id,))
        rows = db.fetchall()
        bookings = [_row_to_booking_dict(dict(r)) for r in rows]

        # Compute group totals
        group_total = sum(b["total_amount"] or 0 for b in bookings)
        group_paid = sum(b["amount_paid"] for b in bookings)
        group_advance = sum(b["advance_amount"] for b in bookings)
        active_count = sum(1 for b in bookings if b["status"] in ("Booked", "Reserved", "Active"))
        checked_out_count = sum(1 for b in bookings if b["status"] == "Checked-out")

        # Customer info from first row
        customer_info = {}
        if rows:
            first_row = dict(rows[0])
            customer_info = {
                "name": first_row.get("customer_name", ""),
                "email": first_row.get("customer_email", ""),
                "phone": first_row.get("customer_phone", ""),
            }

    return templates.TemplateResponse("group-overview.html", {
        "request": request,
        "group": dict(group),
        "bookings": bookings,
        "customer": customer_info,
        "group_total": round(group_total, 2),
        "group_paid": round(group_paid, 2),
        "group_advance": round(group_advance, 2),
        "group_balance": round(max(0, group_total - max(group_paid, group_advance)), 2),
        "active_count": active_count,
        "checked_out_count": checked_out_count,
    })


# ---------- Group checkout all ----------
@router.post("/group/{group_id}/checkout-all")
def group_checkout_all(group_id: int, payment_method: str = Form("")):
    """Checkout all active/booked rooms in a group at once."""
    now_dt = datetime.now()
    now_str = now_dt.isoformat()

    with get_db() as db:
        db.execute("""
            SELECT * FROM bookings
            WHERE group_id = ? AND status IN ('Booked', 'Active')
            ORDER BY room_number
        """, (group_id,))
        active_bookings = db.fetchall()

        if not active_bookings:
            return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)

        from routers.settings import get_setting
        tax_pct_str = get_setting("tax_percent", "10")
        try:
            tax_rate = float(tax_pct_str) / 100.0
        except:
            tax_rate = 0.10

        for b in active_bookings:
            # Reuse same checkout calculation logic from single-room checkout
            try:
                charges = json.loads(b["extra_charges"] or "[]")
            except:
                charges = []

            room_rate = float(b["room_rate"])
            check_in_dt = _dt_parse(b["check_in"])
            booked_nights = int(b["num_nights"])

            if check_in_dt:
                raw_days = max(1, (now_dt.date() - check_in_dt.date()).days)
                actual_nights = min(booked_nights, raw_days)
            else:
                actual_nights = booked_nights

            # Late checkout fee
            expected_out = _dt_parse(b["expected_check_out"])
            if expected_out and (now_dt - expected_out).total_seconds() > 7200:
                late_hours = int((now_dt - expected_out).total_seconds() / 3600)
                fee = min(late_hours * (room_rate / 24) * 1.5, room_rate)
                charges.append({
                    "description": f"Late Checkout Fee ({late_hours}h)",
                    "amount": round(fee, 2)
                })

            actual_room_charge = room_rate * actual_nights
            extras_total = sum(float(c.get("amount", 0)) for c in charges)
            actual_subtotal = round(actual_room_charge + extras_total, 2)
            tax_amount = round(actual_subtotal * tax_rate, 2)
            grand_total = round(actual_subtotal + tax_amount, 2)

            advance_amount = float(b["advance_amount"] or 0)
            remaining_balance = round(grand_total - advance_amount, 2)

            if remaining_balance < 0:
                charges.append({
                    "description": "Refund (advance exceeded charges)",
                    "amount": -abs(remaining_balance)
                })

            # Update booking
            db.execute("""
                UPDATE bookings
                SET actual_check_out = ?, extra_charges = ?, total_amount = ?,
                    num_nights = ?, status = 'Checked-out', updated_at = ?
                WHERE id = ?
            """, (now_str, json.dumps(charges), grand_total, actual_nights, now_str, b["id"]))

            # Payment status
            if remaining_balance > 0:
                new_amount_paid = advance_amount + remaining_balance
                db.execute("UPDATE bookings SET amount_paid = ?, payment_status = 'Paid' WHERE id = ?",
                           (new_amount_paid, b["id"]))
                pm = payment_method or "Cash"
                db.execute("""
                    INSERT INTO payments (booking_id, amount, payment_method, payment_type, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (b["id"], remaining_balance, pm, 'checkout',
                      f'Group #{group_id} checkout', now_str))
                _create_financial_transaction(
                    db, 'income', remaining_balance, 'Bookings',
                    f"Checkout Balance (Booking #{b['id']}, Group #{group_id})", b["id"]
                )
            elif remaining_balance == 0:
                db.execute("UPDATE bookings SET payment_status = 'Paid' WHERE id = ?", (b["id"],))
            else:
                db.execute("UPDATE bookings SET payment_status = 'Refunded' WHERE id = ?", (b["id"],))
                _create_financial_transaction(
                    db, 'expense', abs(remaining_balance), 'Refunds',
                    f"Overpayment Refund (Booking #{b['id']}, Group #{group_id})", b["id"]
                )

            _mark_room_status(db, b["room_number"], status="Available", housekeeping="Dirty")

    return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)


# ---------- Group check-in all reserved rooms ----------
@router.post("/group/{group_id}/checkin-all")
def group_checkin_all(group_id: int):
    """Activate all reserved bookings in a group (check-in all rooms)."""
    now_dt = datetime.now()
    now_str = now_dt.isoformat()

    with get_db() as db:
        db.execute("""
            SELECT * FROM bookings
            WHERE group_id = ? AND status = 'Reserved'
            ORDER BY room_number
        """, (group_id,))
        reserved = db.fetchall()

        if not reserved:
            return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)

        for b in reserved:
            # Prevent early check-in if room is currently occupied or unavailable
            db.execute("SELECT status FROM rooms WHERE room_number = ?", (str(b["room_number"]),))
            room = db.fetchone()
            if room and room["status"] in ("Occupied", "Maintenance", "Out of Order"):
                continue  # Skip this room according to user instructions

            # Update check-in time to now, recalculate expected checkout
            booked_nights = int(b["num_nights"])
            from routers.settings import get_setting
            checkout_time_str = get_setting("checkout_time", "11:00")
            try:
                ch_hour, ch_min = map(int, checkout_time_str.split(':'))
            except:
                ch_hour, ch_min = 11, 0

            expected_out_dt = (now_dt + timedelta(days=booked_nights)).replace(
                hour=ch_hour, minute=ch_min, second=0, microsecond=0
            )

            db.execute("""
                UPDATE bookings
                SET status = 'Booked',
                    check_in = ?,
                    expected_check_out = ?,
                    updated_at = ?
                WHERE id = ?
            """, (now_str, expected_out_dt.isoformat(), now_str, b["id"]))

            # Mark room as occupied
            _mark_room_status(db, str(b["room_number"]), status="Occupied")

    return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)


# ==================== GROUP INVOICE ENDPOINTS ====================

def _build_group_bookings(db, group_id):
    """Fetch all bookings for a group with full context."""
    db.execute("""
        SELECT b.*, c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone
        FROM bookings b
        LEFT JOIN customers c ON c.id = b.customer_id
        WHERE b.group_id = ?
        ORDER BY b.room_number
    """, (group_id,))
    return db.fetchall()


@router.get("/group/{group_id}/invoice", response_class=HTMLResponse)
def group_invoice_combined(group_id: int, request: Request):
    """Combined invoice — all rooms, single grand total."""
    from routers.settings import get_setting
    with get_db() as db:
        db.execute("SELECT * FROM booking_groups WHERE id = ?", (group_id,))
        group = db.fetchone()
        if not group:
            return HTMLResponse("Group not found", status_code=404)

        rows = _build_group_bookings(db, group_id)
        if not rows:
            return HTMLResponse("No bookings in this group", status_code=404)

    # Build per-room line items
    tax_pct_str = get_setting("tax_percent", "10")
    try:
        tax_rate = float(tax_pct_str) / 100.0
    except:
        tax_rate = 0.10

    room_sections = []
    combined_subtotal = 0.0
    combined_charges = []
    total_advance = 0.0
    total_paid = 0.0

    for row in rows:
        try:
            charges = json.loads(row["extra_charges"] or "[]")
        except:
            charges = []

        room_amount = float(row["room_rate"]) * int(row["num_nights"])
        extras = sum(float(c.get("amount", 0)) for c in charges)

        room_sections.append({
            "room_number": row["room_number"],
            "booking_id": row["id"],
            "nights": int(row["num_nights"]),
            "rate": float(row["room_rate"]),
            "room_amount": room_amount,
            "charges": charges,
            "status": row["status"],
        })

        combined_subtotal += room_amount + extras
        combined_charges.extend(charges)
        total_advance += float(row["advance_amount"] or 0)
        total_paid += float(row["amount_paid"] or 0)

    combined_tax = round(combined_subtotal * tax_rate, 2)
    combined_total = round(combined_subtotal + combined_tax, 2)
    effective_paid = max(total_paid, total_advance)
    combined_balance = round(max(0, combined_total - effective_paid), 2)
    combined_refund = round(max(0, total_advance - combined_total), 2)

    # Customer info from first row
    first_row = dict(rows[0]); customer = {"name": first_row.get("customer_name", ""), "email": first_row.get("customer_email", ""), "phone": first_row.get("customer_phone", "")}
    hotel = {
        "name": get_setting("hotel_name", "HavenDesk"),
        "address": get_setting("hotel_address", "123 Example Rd, City"),
        "email": get_setting("hotel_email", "billing@havendesk.example"),
        "phone": get_setting("hotel_phone", "+880 1XXX-XXXXXX"),
    }

    return templates.TemplateResponse("group-invoice.html", {
        "request": request,
        "group": dict(group),
        "mode": "combined",
        "room_sections": room_sections,
        "customer": customer,
        "hotel": hotel,
        "subtotal": round(combined_subtotal, 2),
        "tax": combined_tax,
        "tax_rate_pct": int(tax_rate * 100),
        "grand_total": combined_total,
        "advance_amount": total_advance,
        "amount_paid": total_paid,
        "balance_due": combined_balance,
        "refund_due": combined_refund,
        "currency": get_setting("currency_symbol", "$"),
        "generated_at": datetime.now(),
    })


@router.get("/group/{group_id}/invoice/per-room", response_class=HTMLResponse)
def group_invoice_per_room(group_id: int, request: Request):
    """Per-room invoice — separate section per room with individual totals."""
    from routers.settings import get_setting
    with get_db() as db:
        db.execute("SELECT * FROM booking_groups WHERE id = ?", (group_id,))
        group = db.fetchone()
        if not group:
            return HTMLResponse("Group not found", status_code=404)
        rows = _build_group_bookings(db, group_id)
        if not rows:
            return HTMLResponse("No bookings in this group", status_code=404)

    tax_pct_str = get_setting("tax_percent", "10")
    try:
        tax_rate = float(tax_pct_str) / 100.0
    except:
        tax_rate = 0.10

    room_invoices = []
    group_grand_total = 0.0

    for row in rows:
        try:
            charges = json.loads(row["extra_charges"] or "[]")
        except:
            charges = []

        room_amount = float(row["room_rate"]) * int(row["num_nights"])
        extras = sum(float(c.get("amount", 0)) for c in charges)
        subtotal = round(room_amount + extras, 2)
        tax = round(subtotal * tax_rate, 2)
        total = round(subtotal + tax, 2)
        group_grand_total += total

        room_invoices.append({
            "booking_id": row["id"],
            "room_number": row["room_number"],
            "nights": int(row["num_nights"]),
            "rate": float(row["room_rate"]),
            "room_amount": room_amount,
            "charges": charges,
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
            "advance": float(row["advance_amount"] or 0),
            "paid": float(row["amount_paid"] or 0),
            "balance": round(max(0, total - max(float(row["amount_paid"] or 0), float(row["advance_amount"] or 0))), 2),
            "status": row["status"],
            "check_in": row["check_in"],
            "check_out": row["expected_check_out"],
        })

    first_row = dict(rows[0]); customer = {"name": first_row.get("customer_name", ""), "email": first_row.get("customer_email", ""), "phone": first_row.get("customer_phone", "")}
    hotel = {
        "name": get_setting("hotel_name", "HavenDesk"),
        "address": get_setting("hotel_address", "123 Example Rd, City"),
        "email": get_setting("hotel_email", "billing@havendesk.example"),
        "phone": get_setting("hotel_phone", "+880 1XXX-XXXXXX"),
    }

    return templates.TemplateResponse("group-invoice.html", {
        "request": request,
        "group": dict(group),
        "mode": "per_room",
        "room_invoices": room_invoices,
        "customer": customer,
        "hotel": hotel,
        "tax_rate_pct": int(tax_rate * 100),
        "group_grand_total": round(group_grand_total, 2),
        "currency": get_setting("currency_symbol", "$"),
        "generated_at": datetime.now(),
    })


@router.get("/group/{group_id}/invoice/per-day", response_class=HTMLResponse)
def group_invoice_per_day(group_id: int, request: Request):
    """Per-day invoice — charges grouped by calendar day across all rooms."""
    from routers.settings import get_setting
    with get_db() as db:
        db.execute("SELECT * FROM booking_groups WHERE id = ?", (group_id,))
        group = db.fetchone()
        if not group:
            return HTMLResponse("Group not found", status_code=404)
        rows = _build_group_bookings(db, group_id)
        if not rows:
            return HTMLResponse("No bookings in this group", status_code=404)

    tax_pct_str = get_setting("tax_percent", "10")
    try:
        tax_rate = float(tax_pct_str) / 100.0
    except:
        tax_rate = 0.10

    # Build day-by-day breakdown
    from collections import OrderedDict
    daily_breakdown = OrderedDict()
    overall_subtotal = 0.0
    total_advance = 0.0
    total_paid = 0.0

    for raw_row in rows:
        row = dict(raw_row)
        check_in_dt = _dt_parse(row["check_in"])
        booked_nights = int(row["num_nights"])
        rate = float(row["room_rate"])
        room_number = row["room_number"]
        total_advance += float(row["advance_amount"] or 0)
        total_paid += float(row["amount_paid"] or 0)

        # Use actual stay days for checked-out rooms
        if row["status"] == "Checked-out" and row.get("actual_check_out"):
            actual_out_dt = _dt_parse(row["actual_check_out"])
            if actual_out_dt and check_in_dt:
                nights = max(1, (actual_out_dt.date() - check_in_dt.date()).days)
            else:
                nights = booked_nights
        elif row["status"] == "Reserved":
            # Not checked in yet — skip day-breakdown, just add advance to totals
            continue
        else:
            # Active/Booked: cap to today (don't show future days)
            today = datetime.now().date()
            if check_in_dt:
                days_so_far = (today - check_in_dt.date()).days
                nights = max(1, min(booked_nights, days_so_far + 1))  # include today
            else:
                nights = booked_nights

        if check_in_dt:
            for d in range(nights):
                day_dt = check_in_dt + timedelta(days=d)
                day_key = day_dt.strftime("%Y-%m-%d")
                if day_key not in daily_breakdown:
                    daily_breakdown[day_key] = {"date": day_key, "line_items": [], "day_total": 0.0}
                daily_breakdown[day_key]["line_items"].append({
                    "description": f"Room {room_number}",
                    "amount": rate,
                })
                daily_breakdown[day_key]["day_total"] += rate
                overall_subtotal += rate

        # Add extra charges on the check-in date (or last day)
        try:
            charges = json.loads(row["extra_charges"] or "[]")
        except:
            charges = []
        if charges and check_in_dt:
            # Place charges on the checkout date or last active day
            charge_day = (check_in_dt + timedelta(days=max(0, nights - 1))).strftime("%Y-%m-%d")
            if charge_day not in daily_breakdown:
                daily_breakdown[charge_day] = {"date": charge_day, "line_items": [], "day_total": 0.0}
            for c in charges:
                amt = float(c.get("amount", 0))
                daily_breakdown[charge_day]["line_items"].append({
                    "description": f"Room {room_number}: {c.get('description', 'Charge')}",
                    "amount": amt,
                })
                daily_breakdown[charge_day]["day_total"] += amt
                overall_subtotal += amt

    overall_tax = round(overall_subtotal * tax_rate, 2)
    overall_total = round(overall_subtotal + overall_tax, 2)
    effective_paid = max(total_paid, total_advance)
    overall_balance = round(max(0, overall_total - effective_paid), 2)

    first_row = dict(rows[0]); customer = {"name": first_row.get("customer_name", ""), "email": first_row.get("customer_email", ""), "phone": first_row.get("customer_phone", "")}
    hotel = {
        "name": get_setting("hotel_name", "HavenDesk"),
        "address": get_setting("hotel_address", "123 Example Rd, City"),
        "email": get_setting("hotel_email", "billing@havendesk.example"),
        "phone": get_setting("hotel_phone", "+880 1XXX-XXXXXX"),
    }

    return templates.TemplateResponse("group-invoice.html", {
        "request": request,
        "group": dict(group),
        "mode": "per_day",
        "daily_breakdown": list(daily_breakdown.values()),
        "customer": customer,
        "hotel": hotel,
        "subtotal": round(overall_subtotal, 2),
        "tax": overall_tax,
        "tax_rate_pct": int(tax_rate * 100),
        "grand_total": overall_total,
        "advance_amount": total_advance,
        "amount_paid": total_paid,
        "balance_due": overall_balance,
        "currency": get_setting("currency_symbol", "$"),
        "generated_at": datetime.now(),
    })


# ==================== GROUP MODIFICATION ENDPOINTS ====================

@router.post("/group/{group_id}/add-room")
def group_add_room(
    group_id: int,
    request: Request,
    room_number: str = Form(...),
    nights: int = Form(...),
    advance_amount: float = Form(0.0),
    payment_method: str = Form(""),
):
    """Add a room to an existing booking group."""
    from urllib.parse import quote
    from routers.settings import get_setting

    now = datetime.now()
    nights = max(1, int(nights))
    advance_amount = float(advance_amount) if advance_amount else 0.0

    try:
        with get_db() as db:
            # 1) Get group info
            db.execute("SELECT * FROM booking_groups WHERE id = ?", (group_id,))
            group = db.fetchone()
            if not group:
                raise ValueError("Group not found.")
            customer_id = group["customer_id"]

            # 2) Validate room
            rate, status = _room_rate_and_status(db, room_number.strip())
            if status == "OutOfService":
                raise ValueError(f"Room {room_number} is out of service.")

            # 3) Use the same check-in dates as the existing group bookings
            db.execute("SELECT check_in, expected_check_out FROM bookings WHERE group_id = ? LIMIT 1", (group_id,))
            ref = db.fetchone()
            if ref and ref["check_in"]:
                check_in_dt = _dt_parse(ref["check_in"]) or now
                # Determine if this is a reservation based on whether check-in is in the future
                is_reservation = check_in_dt.date() > now.date()
            else:
                check_in_dt = now
                is_reservation = False

            checkout_time_str = get_setting("checkout_time", "11:00")
            try:
                ch_hour, ch_min = map(int, checkout_time_str.split(':'))
            except:
                ch_hour, ch_min = 11, 0

            checkout_date = check_in_dt + timedelta(days=nights)
            expected_out_dt = checkout_date.replace(hour=ch_hour, minute=ch_min, second=0, microsecond=0)
            check_in = check_in_dt.isoformat()
            expected_out = expected_out_dt.isoformat()

            booking_status = 'Reserved' if is_reservation else 'Booked'

            if status == "Occupied" and not is_reservation:
                raise ValueError(f"Room {room_number} is currently occupied.")

            # 4) Availability
            availability = check_room_availability(db, room_number.strip(), check_in, expected_out)
            if not availability["available"]:
                raise ValueError(f"Room {room_number}: {availability['message']}")

            # 5) Calculate cost
            subtotal = float(rate) * nights
            try:
                tax_rate = float(get_setting("tax_percent", "10")) / 100.0
            except:
                tax_rate = 0.10
            tax_amount = round(subtotal * tax_rate, 2)
            booking_total = round(subtotal + tax_amount, 2)

            if advance_amount > booking_total:
                raise ValueError(f"Advance ({advance_amount}) exceeds room total ({booking_total})")

            # 6) Insert booking
            db.execute("""
                INSERT INTO bookings (
                    customer_id, room_number, check_in, expected_check_out,
                    room_rate, num_nights, extra_charges, total_amount,
                    status, created_at, updated_at,
                    advance_amount, amount_paid, payment_status,
                    group_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                customer_id, room_number.strip(), check_in, expected_out,
                rate, nights, json.dumps([]), booking_total,
                booking_status, now.isoformat(), now.isoformat(),
                advance_amount, advance_amount,
                "Partial" if advance_amount > 0 else "Unpaid",
                group_id
            ))
            bid = db.execute("SELECT last_insert_rowid() as id").fetchone()["id"]

            # Record advance payment if any
            if advance_amount > 0:
                db.execute("""
                    INSERT INTO payments (booking_id, amount, payment_method, payment_type, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (bid, advance_amount, payment_method, 'advance',
                      f'Added to Group #{group_id}', now.isoformat()))
                _create_financial_transaction(
                    db, 'income', advance_amount, 'Bookings',
                    f"Advance Payment (Booking #{bid}, added to Group #{group_id})", bid
                )

            # 7) Mark room occupied if immediate check-in
            if booking_status == 'Booked':
                _mark_room_status(db, room_number.strip(), status="Occupied")

    except Exception as e:
        error_msg = quote(f"Failed to add room: {e}")
        return RedirectResponse(f"/bookings/group/{group_id}?error={error_msg}", status_code=303)

    return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)


@router.post("/group/{group_id}/remove-room/{booking_id}")
def group_remove_room(
    group_id: int,
    booking_id: int,
    refund_type: str = Form("none"),
    refund_percentage: float = Form(50.0),
):
    """Remove a room from a group by cancelling the individual booking.
    Supports: none, full refund, partial refund, or transfer to remaining rooms.
    """
    now = datetime.now()

    with get_db() as db:
        db.execute("SELECT * FROM bookings WHERE id = ? AND group_id = ?", (booking_id, group_id))
        b = db.fetchone()
        if not b:
            return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)

        # If already checked out or cancelled, do nothing
        if b["status"] in ("Checked-out", "Cancelled"):
            return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)

        # Process refund using existing cancel logic
        amount_paid = float(b["amount_paid"] or 0)
        refund_amount = 0.0

        if amount_paid > 0:
            if refund_type == "full":
                refund_amount = amount_paid
            elif refund_type == "partial":
                pct = max(1, min(99, refund_percentage))
                refund_amount = round(amount_paid * pct / 100, 2)
            elif refund_type == "transfer":
                # Transfer advance to remaining active rooms
                db.execute("""
                    SELECT id, total_amount FROM bookings
                    WHERE group_id = ? AND id != ? AND status IN ('Booked', 'Reserved', 'Active')
                """, (group_id, booking_id))
                active_rooms = db.fetchall()
                if active_rooms:
                    total_remaining = sum(float(r["total_amount"] or 0) for r in active_rooms)
                    if total_remaining > 0:
                        running_sum = 0.0
                        for idx, ar in enumerate(active_rooms):
                            if idx < len(active_rooms) - 1:
                                share = round(amount_paid * (float(ar["total_amount"] or 0) / total_remaining), 2)
                            else:
                                share = round(amount_paid - running_sum, 2)
                            running_sum += share
                            db.execute("""
                                UPDATE bookings
                                SET advance_amount = advance_amount + ?,
                                    amount_paid = amount_paid + ?,
                                    payment_status = 'Partial',
                                    updated_at = ?
                                WHERE id = ?
                            """, (share, share, now.isoformat(), ar["id"]))
                    refund_amount = 0  # No cash refund — transferred
                else:
                    # No active rooms left to transfer to — full refund instead
                    refund_amount = amount_paid

        # Add refund/transfer note for audit trail
        extra_charges = []
        try:
            if b["extra_charges"]:
                extra_charges = json.loads(b["extra_charges"])
        except:
            pass

        if refund_type == "transfer" and refund_amount == 0:
            extra_charges.append({
                "description": f"Advance transferred to remaining rooms",
                "amount": 0,
                "date": now.strftime("%Y-%m-%d %H:%M")
            })
        elif refund_amount > 0:
            extra_charges.append({
                "description": f"Group Room Removal Refund ({refund_type.title()})",
                "amount": -refund_amount,
                "date": now.strftime("%Y-%m-%d %H:%M")
            })

        # Update booking status to Cancelled
        db.execute("""
            UPDATE bookings
            SET status = 'Cancelled',
                updated_at = ?,
                extra_charges = ?
            WHERE id = ?
        """, (now.isoformat(), json.dumps(extra_charges), booking_id))

        # Record refund in finance (only for actual cash refunds, not transfers)
        if refund_amount > 0:
            db.execute("""
                INSERT INTO financial_transactions (type, amount, category, description, date, entered_by)
                VALUES ('expense', ?, 'Refund', ?, ?, 'System')
            """, (refund_amount, f"Refund for room removed from Group #{group_id} (Booking #{booking_id})", now.isoformat()))

        # Release room if it was occupied
        if b["status"] == "Booked":
            _mark_room_status(db, b["room_number"], "Available")

    return RedirectResponse(f"/bookings/group/{group_id}", status_code=303)


# ---------- Group listing page ----------
@router.get("/group-list", response_class=HTMLResponse)
def group_list(request: Request):
    """List all booking groups for easy navigation."""
    with get_db() as db:
        db.execute("""
            SELECT bg.*, c.name AS customer_name,
                   (SELECT COUNT(*) FROM bookings WHERE group_id = bg.id) AS room_count,
                   (SELECT COUNT(*) FROM bookings WHERE group_id = bg.id AND status IN ('Booked', 'Reserved', 'Active')) AS active_count,
                   (SELECT COALESCE(SUM(total_amount), 0) FROM bookings WHERE group_id = bg.id) AS total_amount
            FROM booking_groups bg
            LEFT JOIN customers c ON c.id = bg.customer_id
            WHERE c.id IS NOT NULL
            ORDER BY bg.id DESC
        """)
        groups = [dict(r) for r in db.fetchall()]

    return templates.TemplateResponse("group-list.html", {
        "request": request,
        "groups": groups,
    })


# ---------- Hotel-wide availability for "Book Whole Hotel" ----------
@router.get("/hotel-availability")
def hotel_availability(
    check_in: str = Query(None),
    nights: int = Query(1),
):
    """Return all rooms with availability status for given dates.
    For occupied rooms, also returns when they will be free.
    """
    from routers.settings import get_setting

    now = datetime.now()
    nights = max(1, int(nights))

    if check_in:
        try:
            check_in_dt = datetime.fromisoformat(check_in)
        except:
            check_in_dt = datetime.strptime(check_in, '%Y-%m-%d')
    else:
        check_in_dt = now

    check_out_dt = check_in_dt + timedelta(days=nights)

    with get_db() as db:
        db.execute("""
            SELECT room_number, room_type, price_per_night, status
            FROM rooms
            WHERE status != 'Out of Service'
            ORDER BY CAST(room_number AS INTEGER), room_number
        """)
        all_rooms = db.fetchall()

        available_rooms = []
        unavailable_rooms = []

        for r in all_rooms:
            avail = check_room_availability(db, r["room_number"],
                                            check_in_dt.isoformat(), check_out_dt.isoformat())
            room_info = {
                "room_number": r["room_number"],
                "room_type": r["room_type"],
                "price_per_night": float(r["price_per_night"]),
                "status": r["status"],
            }

            if avail["available"]:
                room_info["available"] = True
                available_rooms.append(room_info)
            else:
                room_info["available"] = False
                room_info["conflict_message"] = avail["message"]
                # Find when this room will be free
                db.execute("""
                    SELECT expected_check_out FROM bookings
                    WHERE room_number = ? AND status NOT IN ('Checked-out', 'Cancelled')
                    ORDER BY expected_check_out DESC LIMIT 1
                """, (r["room_number"],))
                last = db.fetchone()
                if last and last["expected_check_out"]:
                    room_info["free_from"] = last["expected_check_out"][:10]
                unavailable_rooms.append(room_info)

    return JSONResponse({
        "check_in": check_in_dt.strftime("%Y-%m-%d"),
        "check_out": check_out_dt.strftime("%Y-%m-%d"),
        "nights": nights,
        "available": available_rooms,
        "unavailable": unavailable_rooms,
        "available_count": len(available_rooms),
        "total_rooms": len(all_rooms),
    })


@router.get("/group/{group_id}/rooms-json")
def group_rooms_json(group_id: int):
    """Returns the rooms in a group as JSON for the service assignment modal."""
    with get_db() as db:
        rows = _build_group_bookings(db, group_id)
    if not rows:
        return JSONResponse({"rooms": []})
    rooms = [
        {
            "room_number": r["room_number"],
            "status": r["status"],
            "booking_id": r["id"]
        }
        for r in rows
    ]
    return JSONResponse({"rooms": rooms})
