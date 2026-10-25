# main.py
import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from importlib import import_module
from crud import get_dashboard_stats, get_all_rooms
from dependencies import require_auth
from datetime import datetime
import math

# --- Load environment variables ---
load_dotenv()

# --- Initialize app ---
app = FastAPI(title="Hotel Admin Panel")

# --- Add session middleware ---
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-prod")
# No max_age = session cookie dies when browser closes
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

SESSION_TIMEOUT_SECONDS = 600  # 10 minutes inactivity timeout

# --- Exception Handler for Redirects ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    if exc.status_code == 303:
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    return HTMLResponse(content=f"<h1>Error {exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code)

# --- Mount static files and templates ---
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Fix for 'now()' in templates
templates.env.globals['now'] = datetime.now

templates.env.filters['ceil'] = math.ceil
templates.env.filters['floor'] = math.floor
templates.env.filters['round'] = round

# --- Currency symbol global for templates ---
def _get_currency_symbol():
    """Fetch currency symbol from settings DB."""
    try:
        from dependencies import get_db
        with get_db() as db:
            db.execute("SELECT value FROM settings WHERE key = 'currency_symbol'")
            row = db.fetchone()
            return row["value"] if row else "$"
    except Exception:
        return "$"

templates.env.globals['currency'] = _get_currency_symbol

# --- Helper: Safe router import ---
def include_router_safe(module_path: str, prefix: str, tag: str):
    # Dynamically import and include router if the module exists.
    try:
        module = import_module(module_path)
        if hasattr(module, "router"):
            # Apply auth dependency to everything EXCEPT auth router
            deps = [Depends(require_auth)] if tag != "auth" else []
            app.include_router(module.router, prefix=prefix, tags=[tag], dependencies=deps)
            print(f"✅ Included router: {module_path} (Protected: {tag != 'auth'})")
        else:
            print(f"⚠️ No 'router' found in module: {module_path}")
    except ModuleNotFoundError:
        print(f"⚠️ Skipped missing module: {module_path}")
    except Exception as e:
        print(f"❌ Error loading router '{module_path}': {e}")

# --- Include all routers dynamically ---
routers_to_load = [
    ("routers.customers", "/customers", "customers"),
    ("routers.rooms", "/rooms", "rooms"),
    ("routers.bookings", "/bookings", "bookings"),
    ("routers.analytics", "/analytics", "analytics"),
    ("routers.auth", "/auth", "auth"),
    ("routers.inventory", "/inventory", "inventory"),
    ("routers.financials", "/financials", "financials"),
    ("routers.ai", "/ai", "ai"),
    ("routers.services", "/services", "services"),
    ("routers.settings", "/settings", "settings"),
    ("routers.campaigns", "/campaigns", "campaigns"),
]

for modname, prefix, tag in routers_to_load:
    include_router_safe(modname, prefix, tag)

# --- Dashboard route ---
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(require_auth)):
    """
    Dashboard home route.
    Renders dashboard.html with statistics from crud.get_dashboard_stats().
    """
    try:
        stats = get_dashboard_stats()
    except Exception as e:
        print(f"⚠️ Error fetching dashboard stats: {e}")
        stats = {}
    return templates.TemplateResponse("dashboard.html", {"request": request, "stats": stats, "currency": _get_currency_symbol()})

# --- Rooms route with filter support ---


# --- Bookings create route ---
@app.get("/bookings/create", response_class=HTMLResponse)
def create_booking(request: Request, user: dict = Depends(require_auth)):
    """
    Display the new booking creation form with available rooms.
    """
    try:
        rooms = get_all_rooms()
    except Exception as e:
        print(f"⚠️ Error fetching rooms: {e}")
        rooms = []
    return templates.TemplateResponse("bookings-create.html", {"request": request, "rooms": rooms})

# --- Logout redirect bridge ---
@app.get("/logout")
def logout_bridge():
    """
    Redirects to /auth/logout (if exists in auth router).
    """
    return RedirectResponse("/auth/logout", status_code=303)

# --- Login fallback route ---
@app.get("/login", response_class=HTMLResponse)
def login_page_fallback(request: Request):
    """
    Fallback login page in case /auth/login route isn't defined.
    """
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

# --- Startup event ---
@app.on_event("startup")
def on_startup():
    print("🚀 Hotel Admin Panel started successfully!")

# --- Run command ---
# Run with: uvicorn main:app --reload
