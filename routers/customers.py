# customers.py
from fastapi import APIRouter, Request, Form
from urllib.parse import quote
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from crud import list_customers, get_customer, get_customer_bookings, add_customer, update_customer
from dependencies import get_db

router = APIRouter()
from datetime import datetime

templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now

# Default customer types
DEFAULT_CUSTOMER_TYPES = ["Regular", "VIP", "Corporate", "Group", "Walk-in", "Online"]

@router.get("/types")
def get_customer_types():
    """Return all distinct customer types (defaults + custom from DB)."""
    with get_db() as db:
        db.execute("SELECT DISTINCT customer_type FROM customers WHERE customer_type IS NOT NULL AND customer_type != '' ORDER BY customer_type")
        db_types = [r["customer_type"] for r in db.fetchall()]
    
    all_types = list(DEFAULT_CUSTOMER_TYPES)
    for t in db_types:
        if t not in all_types:
            all_types.append(t)
    
    return JSONResponse({"types": all_types})

# --- LIST + SEARCH ---
@router.get("/", response_class=HTMLResponse)
def customers_index(request: Request, q: str | None = None):
    customers = list_customers(q)
    # customers.html expects "customers" list and "q"
    return templates.TemplateResponse(
        "customers.html",
        {"request": request, "customers": customers, "q": q or ""}
    )

# --- JSON LOOKUP API ---
@router.get("/lookup")
def customers_lookup(q: str = ""):
    """JSON API to search customers by name, phone, or NID (id_proof)."""
    if not q.strip():
        return JSONResponse({"results": []})
    
    like = f"%{q.strip()}%"
    with get_db() as db:
        db.execute(
            """
            SELECT id, name, phone, id_proof, email
            FROM customers
            WHERE name LIKE ? OR phone LIKE ? OR id_proof LIKE ?
            LIMIT 10
            """,
            (like, like, like),
        )
        rows = db.fetchall()
    
    results = [
        {
            "id": r["id"],
            "name": r["name"],
            "phone": r["phone"] or "",
            "id_proof": r["id_proof"] or "",
            "email": r["email"] or "",
        }
        for r in rows
    ]
    return JSONResponse({"results": results})


# --- JSON QUICK ADD API ---
@router.post("/quick-add")
def customers_quick_add(
    name: str = Form(...),
    phone: str = Form(None),
    id_proof: str = Form(None)
):
    """JSON API to quickly create a basic customer."""
    from datetime import datetime
    name = name.strip()
    phone = (phone or "").strip() or None
    id_proof = (id_proof or "").strip() or None
    
    if not name:
        return JSONResponse({"error": "Name is required"}, status_code=400)
    
    now = datetime.now().isoformat()
    with get_db() as db:
        # Check if customer with same phone or NID proof already exists to prevent duplicate insertion
        if phone:
            db.execute("SELECT id, name, phone, id_proof FROM customers WHERE phone = ?", (phone,))
            existing = db.fetchone()
            if existing:
                return JSONResponse({
                    "id": existing["id"],
                    "name": existing["name"],
                    "phone": existing["phone"] or "",
                    "id_proof": existing["id_proof"] or "",
                    "message": "Found existing customer with same phone number"
                })
        if id_proof:
            db.execute("SELECT id, name, phone, id_proof FROM customers WHERE id_proof = ?", (id_proof,))
            existing = db.fetchone()
            if existing:
                return JSONResponse({
                    "id": existing["id"],
                    "name": existing["name"],
                    "phone": existing["phone"] or "",
                    "id_proof": existing["id_proof"] or "",
                    "message": "Found existing customer with same ID proof"
                })

        db.execute(
            """
            INSERT INTO customers (name, phone, id_proof, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, phone, id_proof, now, now),
        )
        db.execute("SELECT last_insert_rowid() AS lid")
        rid = db.fetchone()["lid"]
        
    return JSONResponse({
        "id": rid,
        "name": name,
        "phone": phone or "",
        "id_proof": id_proof or "",
        "message": "Customer created successfully"
    })


# --- DETAIL VIEW (/customers/{id}) ---
@router.get("/{customer_id}", response_class=HTMLResponse)
def customers_detail(request: Request, customer_id: int):
    customer = get_customer(customer_id)
    if not customer:
        return RedirectResponse("/customers", status_code=303)
    bookings = get_customer_bookings(customer_id)
    # Template's detail branch checks: detail and customers|length == 1
    return templates.TemplateResponse(
        "customers.html",
        {
            "request": request,
            "detail": True,
            "customers": [customer],
            "bookings": bookings
        },
    )


# --- ADD (matches your form in customers.html) ---
@router.post("/add")
def customers_add(
    name: str = Form(...),
    email: str = Form(None),
    phone: str = Form(None),
    id_proof: str = Form(None),
    address: str = Form(None),
    customer_type: str = Form(None),
):
    # Validate: at least email or phone is required
    has_email = bool((email or "").strip())
    has_phone = bool((phone or "").strip())
    if not has_email and not has_phone:
        return RedirectResponse(f"/customers?error={quote('Either email or phone number is required.')}", status_code=303)
    add_customer(name, email, phone, id_proof, address, customer_type)
    return RedirectResponse("/customers", status_code=303)

# --- UPDATE (matches customers.html form: action="/customers/update" with hidden customer_id) ---
@router.post("/update")
def customers_update(
    customer_id: int = Form(...),
    name: str | None = Form(None),
    email: str | None = Form(None),
    phone: str | None = Form(None),
    id_proof: str | None = Form(None),
    address: str | None = Form(None),
    customer_type: str | None = Form(None),
):
    update_customer(customer_id, name, email, phone, id_proof, address, customer_type)
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)