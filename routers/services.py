from fastapi import APIRouter, Form, Request, Depends
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from dependencies import get_db
from datetime import datetime

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now

from routers.settings import get_setting

# GET services (JSON API for Frontend)
@router.get("/")
def services_index():
    """Return services catalog as JSON for booking page"""
    with get_db() as db:
        db.execute("SELECT id, name, price, category, priority, description FROM services_catalog ORDER BY priority DESC, name")
        rows = db.fetchall()
        services = [dict(r) for r in rows]
    return JSONResponse({"services": services})

# Admin Interface
@router.get("/admin", response_class=HTMLResponse)
def services_admin_ui(request: Request, sort_by: str = "priority", sort_order: str = "desc"):
    # Validate sort parameters
    allowed_sort_cols = {"name": "name", "price": "price", "priority": "priority"}
    sort_col = allowed_sort_cols.get(sort_by, "priority")
    order = "DESC" if sort_order.lower() == "desc" else "ASC"
    
    with get_db() as db:
        db.execute(f"SELECT * FROM services_catalog ORDER BY {sort_col} {order}, id DESC")
        rows = db.fetchall()
        services = [dict(r) for r in rows]
        
    return templates.TemplateResponse("services-admin.html", {
        "request": request, 
        "services": services,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "currency": get_setting("currency_symbol", "$")
    })

@router.get("/manage", response_class=HTMLResponse)
def manage_services(request: Request):
    """Service management page - add services to active bookings"""
    with get_db() as db:
        # Get services catalog
        db.execute("SELECT * FROM services_catalog ORDER BY name")
        services = db.fetchall()
        
        # Get active bookings (include group_id so frontend knows if it's a group booking)
        db.execute("""
            SELECT b.id, b.room_number, b.check_in, b.total_amount,
                   b.group_id, c.name AS customer_name
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            WHERE b.status IN ('Active', 'Booked')
            ORDER BY b.check_in DESC
        """)
        active_bookings = db.fetchall()
    
    return templates.TemplateResponse("services-manage.html", {
        "request": request,
        "services": services,
        "active_bookings": active_bookings,
        "currency": get_setting("currency_symbol", "$")
    })

@router.post("/add-to-booking")
def add_service_to_booking(
    booking_id: int = Form(...),
    service_id: int = Form(...),
    quantity: int = Form(1),
    service_date: str = Form(None),
    room_number: str = Form(None),
):
    """Add a service to an existing booking with optional date and room targeting."""
    import json
    from datetime import datetime
    
    with get_db() as db:
        # Get service details
        db.execute("SELECT * FROM services_catalog WHERE id = ?", (service_id,))
        service = db.fetchone()
        if not service:
            return RedirectResponse("/services/manage", status_code=303)
        
        # Get booking
        db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        booking = db.fetchone()
        if not booking:
            return RedirectResponse("/services/manage", status_code=303)
        
        # Parse existing charges
        charges = []
        if booking["extra_charges"]:
            try:
                charges = json.loads(booking["extra_charges"]) or []
            except:
                charges = []
        
        # Build descriptive label including date and room
        service_total = service["price"] * quantity
        service_desc = service['name']
        if quantity > 1:
            service_desc += f" x{quantity}"
        if service_date:
            service_desc += f" [{service_date}]"
        if room_number:
            service_desc += f" (Room {room_number})"
        
        charges.append({"description": service_desc, "amount": service_total})
        
        # Recalculate total using room_rate * num_nights + all charges
        new_total = float(booking["room_rate"]) * int(booking["num_nights"])
        for charge in charges:
            new_total += charge["amount"]
        
        # Update booking
        now = datetime.now().isoformat()
        db.execute("""
            UPDATE bookings
            SET extra_charges = ?, total_amount = ?, updated_at = ?
            WHERE id = ?
        """, (json.dumps(charges), new_total, now, booking_id))
        
        # Create financial transaction
        db.execute("""
            INSERT INTO financial_transactions (type, amount, category, description, date)
            VALUES ('income', ?, 'Services', ?, ?)
        """, (service_total, f"{service_desc} (Booking #{booking_id})", now))
    
    return RedirectResponse("/services/manage", status_code=303)

# CRUD Actions
@router.post("/add")
def add_service(
    name: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    priority: str = Form("Normal"),
    description: str = Form(""),
):
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute("""
            INSERT INTO services_catalog (name, price, category, priority, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, price, category, priority, description, now))
    return RedirectResponse("/services/admin", status_code=303)

@router.post("/delete")
def delete_service(id: int = Form(...)):
    with get_db() as db:
        db.execute("DELETE FROM services_catalog WHERE id = ?", (id,))
    return RedirectResponse("/services/admin", status_code=303)

@router.post("/update")
def update_service(
    id: int = Form(...),
    name: str = Form(...),
    price: float = Form(...),
    priority: str = Form(...),
):
    with get_db() as db:
        db.execute("""
            UPDATE services_catalog 
            SET name=?, price=?, priority=? 
            WHERE id=?
        """, (name, price, priority, id))
    return RedirectResponse("/services/admin", status_code=303)
