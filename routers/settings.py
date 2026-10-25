from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dependencies import get_db
from datetime import datetime

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now

def get_setting(key: str, default: str = None):
    with get_db() as db:
        db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = db.fetchone()
        return row["value"] if row else default

@router.get("/", response_class=HTMLResponse)
def settings_page(request: Request):
    settings = {}
    with get_db() as db:
        db.execute("SELECT * FROM settings")
        rows = db.fetchall()
        for r in rows:
            settings[r["key"]] = r["value"]
            
    return templates.TemplateResponse("settings.html", {"request": request, "settings": settings})

@router.post("/update")
def update_settings(
    checkout_time: str = Form(...),
    tax_percent: str = Form("10"),
    late_checkin_penalty_percent: str = Form("0"),
    hotel_name: str = Form("HavenDesk"),
    hotel_email: str = Form(""),
    hotel_phone: str = Form(""),
    hotel_address: str = Form(""),
    currency_symbol: str = Form("$")
):
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute("REPLACE INTO settings (key, value, updated_at) VALUES ('checkout_time', ?, ?)", (checkout_time, now))
        db.execute("REPLACE INTO settings (key, value, updated_at) VALUES ('tax_percent', ?, ?)", (tax_percent, now))
        db.execute("REPLACE INTO settings (key, value, updated_at) VALUES ('late_checkin_penalty_percent', ?, ?)", (late_checkin_penalty_percent, now))
        db.execute("REPLACE INTO settings (key, value, updated_at) VALUES ('hotel_name', ?, ?)", (hotel_name, now))
        db.execute("REPLACE INTO settings (key, value, updated_at) VALUES ('hotel_email', ?, ?)", (hotel_email, now))
        db.execute("REPLACE INTO settings (key, value, updated_at) VALUES ('hotel_phone', ?, ?)", (hotel_phone, now))
        db.execute("REPLACE INTO settings (key, value, updated_at) VALUES ('hotel_address', ?, ?)", (hotel_address, now))
        db.execute("REPLACE INTO settings (key, value, updated_at) VALUES ('currency_symbol', ?, ?)", (currency_symbol, now))
        
    return RedirectResponse("/settings?success=1", status_code=303)
