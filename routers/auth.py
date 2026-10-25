# routers/auth.py
from fastapi import APIRouter, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from dependencies import get_db
import bcrypt
from datetime import datetime

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now

@router.get("/login")
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with get_db() as db:
        db.execute("SELECT id, username, password_hash, role FROM users WHERE username = ?", (username,))
        row = db.fetchone()
    if not row:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    stored = row["password_hash"]
    if isinstance(stored, str):
        stored = stored.encode("utf-8")
    if not bcrypt.checkpw(password.encode("utf-8"), stored):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    import time
    request.session["user"] = {"id": row["id"], "username": row["username"], "role": row["role"]}
    request.session["last_activity"] = time.time()  # Initialize activity timestamp
    return RedirectResponse("/", status_code=303)

@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)