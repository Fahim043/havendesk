# routers/campaigns.py
"""
Campaigns module — mail-merge style messaging to filtered customer audiences.

Flow:
  1. Build an audience with dynamic filter rules (visit dates, spend, services, ...)
  2. Review the result list, add / remove people manually
  3. Write one message with merge fields ({name}, {last_visit}, ...)
  4. Send via Email (any SMTP) and/or WhatsApp (click-to-send or Cloud API)
Every campaign and every recipient result is stored for history.
"""
import json
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dependencies import get_db
from routers.settings import get_setting

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["now"] = datetime.now

# ---------------------------------------------------------------------------
# Sender settings
# ---------------------------------------------------------------------------
SETTING_DEFAULTS = {
    # Email (generic SMTP)
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_security": "starttls",       # starttls | ssl | none
    "smtp_username": "",
    "smtp_password": "",
    "from_email": "",
    "from_name": "",
    "reply_to": "",
    "email_delay_seconds": "1",
    # WhatsApp
    "wa_sender_number": "",
    "wa_mode": "manual",               # manual (click-to-send) | api (Cloud API)
    "wa_default_country_code": "880",
    "wa_api_version": "v19.0",
    "wa_phone_number_id": "",
    "wa_access_token": "",
    "wa_message_type": "text",         # text | template
    "wa_template_name": "",
    "wa_template_lang": "en",
    "wa_template_params": "name",
}
SECRET_KEYS = {"smtp_password", "wa_access_token"}

DEFAULT_CUSTOMER_TYPES = ["Regular", "VIP", "Corporate", "Group", "Walk-in", "Online"]
DEFAULT_PAYMENT_METHODS = ["Cash", "Card", "bKash", "Nagad", "Bank Transfer"]
STAY_STATUSES = ("Booked", "Active", "Checked-out")


def _get_cfg() -> dict:
    cfg = dict(SETTING_DEFAULTS)
    with get_db() as db:
        db.execute("SELECT key, value FROM campaign_settings")
        for r in db.fetchall():
            cfg[r["key"]] = r["value"] if r["value"] is not None else ""
    return cfg


def _email_ready(cfg) -> bool:
    return bool(cfg.get("smtp_host") and cfg.get("from_email"))


def _wa_api_ready(cfg) -> bool:
    return bool(cfg.get("wa_phone_number_id") and cfg.get("wa_access_token"))


def _effective_wa_mode(cfg) -> str:
    return "api" if cfg.get("wa_mode") == "api" and _wa_api_ready(cfg) else "manual"


# ---------------------------------------------------------------------------
# Customer profiles (aggregated stats used for filtering + merge fields)
# ---------------------------------------------------------------------------
def _parse_dt(s):
    if not s:
        return None
    s = str(s)
    try:
        return datetime.fromisoformat(s[:19])
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            return None


def _is_service_charge(desc: str, amount: float) -> bool:
    d = desc.lower()
    if amount <= 0:
        return False
    return not any(k in d for k in ("discount", "refund", "penalty", "late check"))


def _build_profiles() -> dict:
    with get_db() as db:
        db.execute("SELECT * FROM customers")
        customers = [dict(r) for r in db.fetchall()]
        db.execute("SELECT room_number, room_type FROM rooms")
        room_types = {r["room_number"]: r["room_type"] for r in db.fetchall()}
        db.execute("SELECT * FROM bookings WHERE status IS NULL OR status != 'Cancelled'")
        bookings = [dict(r) for r in db.fetchall()]

        pay_methods = {}
        try:
            db.execute("SELECT booking_id, payment_method FROM payments "
                       "WHERE payment_method IS NOT NULL AND payment_method != ''")
            for r in db.fetchall():
                pay_methods.setdefault(r["booking_id"], set()).add(r["payment_method"])
        except Exception:
            pass

        svc_rows = {}
        try:
            db.execute("SELECT booking_id, service_name FROM services WHERE service_name IS NOT NULL")
            for r in db.fetchall():
                svc_rows.setdefault(r["booking_id"], set()).add(r["service_name"])
        except Exception:
            pass

    names = {c["id"]: c["name"] for c in customers}
    profiles = {}
    for c in customers:
        profiles[c["id"]] = {
            "id": c["id"],
            "name": (c.get("name") or "").strip(),
            "email": (c.get("email") or "").strip(),
            "phone": (c.get("phone") or "").strip(),
            "customer_type": (c.get("customer_type") or "").strip(),
            "created_at": _parse_dt(c.get("created_at")),
            "opted_out": bool(c.get("marketing_opt_out") or 0),
            "stays": 0, "nights": 0, "total_spent": 0.0,
            "visits": [], "room_types": {}, "services": set(), "payment_methods": set(),
            "max_pax": 0, "was_referred": False, "referred_by": set(), "referrals_made": 0,
            "group_guest": False, "in_house": False, "upcoming": False,
        }

    for b in bookings:
        ref_id = b.get("reference_id")
        if ref_id and ref_id in profiles:
            profiles[ref_id]["referrals_made"] += 1

        p = profiles.get(b.get("customer_id"))
        if not p:
            continue
        status = b.get("status") or ""
        if ref_id:
            p["was_referred"] = True
            if names.get(ref_id):
                p["referred_by"].add(names[ref_id])
        if status == "Reserved":
            p["upcoming"] = True
            continue
        if status not in STAY_STATUSES:
            continue

        p["stays"] += 1
        p["nights"] += int(b.get("num_nights") or 0)
        p["total_spent"] += float(b.get("total_amount") or 0)
        ci = _parse_dt(b.get("check_in"))
        if ci:
            p["visits"].append(ci)
        rt = room_types.get(b.get("room_number"))
        if rt:
            p["room_types"][rt] = p["room_types"].get(rt, 0) + 1
        p["max_pax"] = max(p["max_pax"], int(b.get("pax") or 1))
        if b.get("group_id"):
            p["group_guest"] = True
        if status in ("Booked", "Active"):
            p["in_house"] = True
        p["payment_methods"] |= pay_methods.get(b["id"], set())
        p["services"] |= svc_rows.get(b["id"], set())
        try:
            for ch in json.loads(b.get("extra_charges") or "[]") or []:
                desc = str(ch.get("description") or ch.get("title") or "").strip()
                amt = float(ch.get("amount") or 0)
                if desc and _is_service_charge(desc, amt):
                    p["services"].add(desc)
        except Exception:
            pass

    now = datetime.now()
    for p in profiles.values():
        p["visits"].sort()
        p["first_visit"] = p["visits"][0] if p["visits"] else None
        p["last_visit"] = p["visits"][-1] if p["visits"] else None
        p["days_since_last"] = (now - p["last_visit"]).days if p["last_visit"] else None
        p["avg_spend"] = round(p["total_spent"] / p["stays"], 2) if p["stays"] else 0.0
        p["favorite_room_type"] = (max(p["room_types"], key=p["room_types"].get)
                                   if p["room_types"] else "")
    return profiles


# ---------------------------------------------------------------------------
# Filter rule engine
# ---------------------------------------------------------------------------
def _num(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except Exception:
        return None


def _date(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _pair(v):
    if isinstance(v, (list, tuple)):
        v = list(v) + [None, None]
        return v[0], v[1]
    return None, None


def _cmp(actual, op, value):
    if op == "between":
        lo, hi = _pair(value)
        lo, hi = _num(lo), _num(hi)
        if lo is None and hi is None:
            return True
        if actual is None:
            return False
        return (lo is None or actual >= lo) and (hi is None or actual <= hi)
    v = _num(value)
    if v is None:
        return True  # empty value -> rule ignored
    if actual is None:
        return False
    return {"gte": actual >= v, "lte": actual <= v, "gt": actual > v,
            "lt": actual < v, "eq": actual == v}.get(op, True)


def _wanted(value):
    if isinstance(value, list):
        return [str(x).strip().lower() for x in value if str(x).strip()]
    return [str(value).strip().lower()] if value and str(value).strip() else []


def _set_match(have, op, value):
    want = _wanted(value)
    if not want:
        return True
    have_l = {str(h).lower() for h in have}
    if op == "all_of":
        return all(w in have_l for w in want)
    if op == "none_of":
        return not any(w in have_l for w in want)
    return any(w in have_l for w in want)


def _eval_rule(p, rule) -> bool:
    f, op, v = rule.get("field"), rule.get("op"), rule.get("value")

    if f == "visit_date":
        lo, hi = _pair(v)
        lo, hi = _date(lo), _date(hi)
        if lo is None and hi is None:
            return True
        hit = any((lo is None or x.date() >= lo) and (hi is None or x.date() <= hi)
                  for x in p["visits"])
        return (not hit) if op == "not_between" else hit

    if f == "customer_since":
        lo, hi = _pair(v)
        lo, hi = _date(lo), _date(hi)
        if lo is None and hi is None:
            return True
        c = p["created_at"].date() if p["created_at"] else None
        return bool(c) and (lo is None or c >= lo) and (hi is None or c <= hi)

    if f == "last_visit_days":
        return _cmp(p["days_since_last"], op, v)
    if f in ("total_spent", "avg_spend", "stays", "nights", "max_pax"):
        return _cmp(p[f], op, v)
    if f == "services":
        return _set_match(p["services"], op, v)
    if f == "room_type":
        return _set_match(set(p["room_types"].keys()), op, v)
    if f == "customer_type":
        return _set_match({p["customer_type"]} if p["customer_type"] else set(), op, v)
    if f == "payment_method":
        return _set_match(p["payment_methods"], op, v)
    if f == "referral":
        return {
            "was_referred": p["was_referred"],
            "not_referred": not p["was_referred"],
            "is_referrer": p["referrals_made"] > 0,
            "not_referrer": p["referrals_made"] == 0,
        }.get(v, True)
    if f == "referred_by":
        q = str(v or "").strip().lower()
        return True if not q else any(q in n.lower() for n in p["referred_by"])
    if f == "referrals_made":
        return _cmp(p["referrals_made"], op, v)
    if f == "flag":
        return {
            "in_house": p["in_house"],
            "not_in_house": not p["in_house"],
            "upcoming": p["upcoming"],
            "no_upcoming": not p["upcoming"],
            "group_guest": p["group_guest"],
            "never_stayed": p["stays"] == 0,
            "has_stayed": p["stays"] > 0,
            "has_email": bool(p["email"]),
            "has_phone": bool(p["phone"]),
        }.get(v, True)
    if f == "name_contains":
        q = str(v or "").strip().lower()
        return True if not q else q in p["name"].lower()
    return True


def _apply_filters(profiles: dict, filters: dict):
    filters = filters or {}
    match = filters.get("match", "all")
    rules = [r for r in (filters.get("rules") or []) if r.get("field")]
    out = []
    for p in profiles.values():
        if rules:
            results = [_eval_rule(p, r) for r in rules]
            if not (all(results) if match == "all" else any(results)):
                continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Merge fields
# ---------------------------------------------------------------------------
MERGE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}|\{(\w+)\}")


def _hotel_info():
    return {
        "hotel_name": get_setting("hotel_name", "Our Hotel") or "Our Hotel",
        "hotel_phone": get_setting("hotel_phone", "") or "",
        "hotel_email": get_setting("hotel_email", "") or "",
        "hotel_address": get_setting("hotel_address", "") or "",
        "currency": get_setting("currency_symbol", "$") or "$",
    }


def _fmt_date(d):
    return d.strftime("%d %b %Y") if d else ""


def _merge_values(p: dict, hotel: dict) -> dict:
    cur = hotel["currency"]
    name = p["name"]
    return {
        "name": name,
        "first_name": name.split()[0] if name else "",
        "email": p["email"],
        "phone": p["phone"],
        "customer_type": p["customer_type"],
        "total_spent": f"{cur}{p['total_spent']:,.0f}",
        "avg_spend": f"{cur}{p['avg_spend']:,.0f}",
        "total_stays": str(p["stays"]),
        "total_nights": str(p["nights"]),
        "first_visit": _fmt_date(p["first_visit"]),
        "last_visit": _fmt_date(p["last_visit"]),
        "days_since_last_visit": "" if p["days_since_last"] is None else str(p["days_since_last"]),
        "favorite_room_type": p["favorite_room_type"],
        "services_used": ", ".join(sorted(p["services"])),
        "hotel_name": hotel["hotel_name"],
        "hotel_phone": hotel["hotel_phone"],
        "hotel_email": hotel["hotel_email"],
        "hotel_address": hotel["hotel_address"],
        "today": _fmt_date(datetime.now()),
    }


def _render(text: str, values: dict) -> str:
    def rep(m):
        key = m.group(1) or m.group(2)
        return values[key] if key in values else m.group(0)
    return MERGE_RE.sub(rep, text or "")


def _public(p: dict, hotel: dict) -> dict:
    return {
        "id": p["id"],
        "name": p["name"],
        "email": p["email"],
        "phone": p["phone"],
        "customer_type": p["customer_type"],
        "stays": p["stays"],
        "nights": p["nights"],
        "total_spent": round(p["total_spent"], 2),
        "last_visit": _fmt_date(p["last_visit"]),
        "days_since_last": p["days_since_last"],
        "opted_out": p["opted_out"],
        "merge": _merge_values(p, hotel),
    }


# ---------------------------------------------------------------------------
# Channels: Email (SMTP) + WhatsApp
# ---------------------------------------------------------------------------
def _norm_phone(raw: str, country_code: str) -> str:
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    cc = re.sub(r"\D", "", country_code or "")
    if cc and digits.startswith(cc) and len(digits) > 10:
        return digits
    if digits.startswith("0"):
        digits = digits[1:]
    return cc + digits if cc else digits


def _wa_link(phone: str, message: str) -> str:
    return f"https://wa.me/{phone}?text={urllib.parse.quote(message or '')}"


class _SmtpSender:
    """Re-uses one SMTP connection for a whole campaign, reconnecting on failure."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def _connect(self):
        cfg = self.cfg
        host = cfg["smtp_host"].strip()
        port = int(cfg.get("smtp_port") or 587)
        sec = cfg.get("smtp_security") or "starttls"
        ctx = ssl.create_default_context()
        if sec == "ssl":
            conn = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            conn = smtplib.SMTP(host, port, timeout=30)
            conn.ehlo()
            if sec == "starttls":
                conn.starttls(context=ctx)
                conn.ehlo()
        if cfg.get("smtp_username"):
            conn.login(cfg["smtp_username"], cfg.get("smtp_password") or "")
        self.conn = conn

    def _build(self, to_addr, subject, body):
        cfg = self.cfg
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject or "(no subject)"
        msg["From"] = formataddr((cfg.get("from_name") or "", cfg["from_email"]))
        msg["To"] = to_addr
        if cfg.get("reply_to"):
            msg["Reply-To"] = cfg["reply_to"]
        html_body = escape(body or "").replace("\n", "<br>")
        html = (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
            'line-height:1.6;color:#222;max-width:600px">' + html_body + "</div>"
        )
        msg.attach(MIMEText(body or "", "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        return msg

    def send(self, to_addr, subject, body):
        msg = self._build(to_addr, subject, body)
        for attempt in range(2):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.sendmail(self.cfg["from_email"], [to_addr], msg.as_string())
                return
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError):
                self.close()
                if attempt == 1:
                    raise

    def close(self):
        try:
            if self.conn:
                self.conn.quit()
        except Exception:
            pass
        self.conn = None


def _wa_api_send(cfg, phone, message, values):
    url = (f"https://graph.facebook.com/{cfg.get('wa_api_version') or 'v19.0'}/"
           f"{cfg['wa_phone_number_id']}/messages")
    if cfg.get("wa_message_type") == "template":
        params = [p.strip() for p in (cfg.get("wa_template_params") or "").split(",") if p.strip()]
        components = []
        if params:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": values.get(k, "") or "-"} for k in params],
            })
        payload = {
            "messaging_product": "whatsapp", "to": phone, "type": "template",
            "template": {
                "name": cfg.get("wa_template_name"),
                "language": {"code": cfg.get("wa_template_lang") or "en"},
                "components": components,
            },
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": phone, "type": "text",
                   "text": {"preview_url": True, "body": message}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {cfg['wa_access_token']}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except Exception:
            pass
        raise RuntimeError(f"WhatsApp API {e.code}: {detail}")


# ---------------------------------------------------------------------------
# Campaign processing (background worker)
# ---------------------------------------------------------------------------
def _finalize_status(cid: int):
    with get_db() as db:
        db.execute("SELECT COUNT(*) AS c FROM campaign_recipients WHERE campaign_id=? AND status='pending'", (cid,))
        pending = db.fetchone()["c"]
        db.execute("SELECT wa_mode, status FROM campaigns WHERE id=?", (cid,))
        row = db.fetchone()
        if not row:
            return
        if pending:
            status = "in_progress"
        else:
            status = "completed"
        db.execute("UPDATE campaigns SET status=?, completed_at=? WHERE id=?",
                   (status, datetime.now().isoformat() if status == "completed" else None, cid))


def _process_campaign(cid: int):
    cfg = _get_cfg()
    hotel_cfg_name = cfg.get("from_name") or get_setting("hotel_name", "") or ""
    cfg["from_name"] = hotel_cfg_name
    with get_db() as db:
        db.execute("SELECT wa_mode FROM campaigns WHERE id=?", (cid,))
        row = db.fetchone()
        wa_mode = row["wa_mode"] if row else "manual"
        db.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id=? AND status='pending' "
            "AND (channel='email' OR (channel='whatsapp' AND ?='api')) ORDER BY id",
            (cid, wa_mode),
        )
        rows = [dict(r) for r in db.fetchall()]

    try:
        delay = max(0.0, float(cfg.get("email_delay_seconds") or 0))
    except Exception:
        delay = 1.0

    profiles = None
    hotel = _hotel_info()
    smtp = _SmtpSender(cfg) if any(r["channel"] == "email" for r in rows) else None
    try:
        for r in rows:
            status, error = "sent", None
            try:
                if r["channel"] == "email":
                    smtp.send(r["address"], r["subject"], r["message"])
                else:
                    if profiles is None:
                        profiles = _build_profiles()
                    p = profiles.get(r["customer_id"])
                    values = _merge_values(p, hotel) if p else {"name": r["name"] or ""}
                    _wa_api_send(cfg, r["address"], r["message"], values)
            except Exception as e:
                status, error = "failed", str(e)[:500]
            with get_db() as db:
                db.execute("UPDATE campaign_recipients SET status=?, error=?, sent_at=? WHERE id=?",
                           (status, error, datetime.now().isoformat(), r["id"]))
            if delay:
                time.sleep(delay)
    finally:
        if smtp:
            smtp.close()
        _finalize_status(cid)


def _start_worker(cid: int):
    threading.Thread(target=_process_campaign, args=(cid,), daemon=True).start()


def _campaign_counts(db, cid):
    db.execute("SELECT status, COUNT(*) AS c FROM campaign_recipients WHERE campaign_id=? GROUP BY status", (cid,))
    counts = {"sent": 0, "failed": 0, "pending": 0, "skipped": 0}
    for r in db.fetchall():
        counts[r["status"]] = r["c"]
    counts["total"] = sum(counts.values())
    return counts


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def campaigns_page(request: Request):
    with get_db() as db:
        db.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 200")
        campaigns = [dict(r) for r in db.fetchall()]
        for c in campaigns:
            c["counts"] = _campaign_counts(db, c["id"])
            c["created_fmt"] = (_parse_dt(c["created_at"]).strftime("%d %b %Y, %I:%M %p")
                                if c.get("created_at") else "")
    cfg = _get_cfg()
    return templates.TemplateResponse("campaigns.html", {
        "request": request,
        "campaigns": campaigns,
        "email_ready": _email_ready(cfg),
        "wa_mode": _effective_wa_mode(cfg),
        "cfg_public": _public_cfg(cfg),
    })


@router.get("/filter-options")
def filter_options():
    with get_db() as db:
        services = set()
        try:
            db.execute("SELECT name FROM services_catalog")
            services |= {r["name"] for r in db.fetchall() if r["name"]}
        except Exception:
            pass
        db.execute("SELECT DISTINCT room_type FROM rooms WHERE room_type IS NOT NULL ORDER BY room_type")
        room_types = [r["room_type"] for r in db.fetchall()]
        db.execute("SELECT DISTINCT customer_type FROM customers "
                   "WHERE customer_type IS NOT NULL AND customer_type != ''")
        ctypes = list(DEFAULT_CUSTOMER_TYPES)
        for r in db.fetchall():
            if r["customer_type"] not in ctypes:
                ctypes.append(r["customer_type"])
        methods = list(DEFAULT_PAYMENT_METHODS)
        try:
            db.execute("SELECT DISTINCT payment_method FROM payments "
                       "WHERE payment_method IS NOT NULL AND payment_method != ''")
            for r in db.fetchall():
                if r["payment_method"] not in methods:
                    methods.append(r["payment_method"])
        except Exception:
            pass
    # Include service names that appear on bookings but are not (or no longer) in the catalog
    for p in _build_profiles().values():
        services |= p["services"]
    return JSONResponse({
        "services": sorted(services, key=str.lower),
        "room_types": room_types,
        "customer_types": ctypes,
        "payment_methods": methods,
        "currency": get_setting("currency_symbol", "$") or "$",
    })


@router.post("/audience")
def audience(payload: dict = Body(...)):
    profiles = _build_profiles()
    matched = _apply_filters(profiles, payload.get("filters") or {})
    hotel = _hotel_info()
    opted_out = [p for p in matched if p["opted_out"]]
    result = [_public(p, hotel) for p in matched if not p["opted_out"]]
    result.sort(key=lambda x: (-x["total_spent"], x["name"].lower()))
    return JSONResponse({
        "customers": result,
        "summary": {
            "matched": len(result),
            "opted_out_excluded": len(opted_out),
            "with_email": sum(1 for x in result if x["email"]),
            "with_phone": sum(1 for x in result if x["phone"]),
        },
    })


@router.post("/profiles")
def profiles_by_ids(payload: dict = Body(...)):
    ids = {int(i) for i in (payload.get("ids") or []) if str(i).isdigit()}
    profiles = _build_profiles()
    hotel = _hotel_info()
    return JSONResponse({"customers": [_public(profiles[i], hotel) for i in ids if i in profiles]})


def _plan_recipients(p, channel, cfg):
    """Return list of (channel, address|None, skip_reason|None) for one customer."""
    cc = cfg.get("wa_default_country_code", "")
    phone = _norm_phone(p["phone"], cc)
    email = p["email"]
    out = []
    if channel in ("email", "both"):
        out.append(("email", email, None) if email else ("email", None, "No email address"))
    if channel in ("whatsapp", "both"):
        out.append(("whatsapp", phone, None) if phone else ("whatsapp", None, "No phone number"))
    if channel == "smart":
        if email:
            out.append(("email", email, None))
        elif phone:
            out.append(("whatsapp", phone, None))
        else:
            out.append(("email", None, "No email or phone"))
    return out


@router.post("/send")
def send_campaign(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip() or f"Campaign {datetime.now():%d %b %Y %H:%M}"
    channel = payload.get("channel") or "email"
    subject = payload.get("subject") or ""
    body = payload.get("body") or ""
    ids = [int(i) for i in (payload.get("customer_ids") or []) if str(i).isdigit()]

    if channel not in ("email", "whatsapp", "both", "smart"):
        return JSONResponse({"error": "Invalid channel."}, status_code=400)
    if not body.strip():
        return JSONResponse({"error": "Message body is empty."}, status_code=400)
    if not ids:
        return JSONResponse({"error": "No recipients selected."}, status_code=400)

    cfg = _get_cfg()
    if channel in ("email", "both", "smart") and not _email_ready(cfg):
        return JSONResponse({"error": "Email sender is not configured. Open 'Sender Settings' and add your SMTP details."},
                            status_code=400)
    if channel in ("email", "both") and not subject.strip():
        return JSONResponse({"error": "Email subject is required."}, status_code=400)

    wa_mode = _effective_wa_mode(cfg)
    profiles = _build_profiles()
    hotel = _hotel_info()
    now = datetime.now().isoformat()

    rows = []
    seen = set()
    for cid in ids:
        if cid in seen or cid not in profiles:
            continue
        seen.add(cid)
        p = profiles[cid]
        if p["opted_out"]:
            continue
        values = _merge_values(p, hotel)
        r_subject = _render(subject, values)
        r_body = _render(body, values)
        for ch, addr, reason in _plan_recipients(p, channel, cfg):
            rows.append((cid, p["name"], ch, addr or "", r_subject if ch == "email" else "",
                         r_body, "skipped" if reason else "pending", reason))

    if not rows:
        return JSONResponse({"error": "None of the selected customers can be contacted."}, status_code=400)

    with get_db() as db:
        db.execute(
            "INSERT INTO campaigns (name, channel, wa_mode, subject, body, filters_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, channel, wa_mode, subject, body, json.dumps(payload.get("filters") or {}), "sending", now),
        )
        db.execute("SELECT last_insert_rowid() AS id")
        campaign_id = db.fetchone()["id"]
        db.executemany(
            "INSERT INTO campaign_recipients (campaign_id, customer_id, name, channel, address, subject, message, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(campaign_id,) + r for r in rows],
        )

    needs_worker = any(r[6] == "pending" and (r[2] == "email" or wa_mode == "api") for r in rows)
    if needs_worker:
        _start_worker(campaign_id)
    else:
        _finalize_status(campaign_id)
    return JSONResponse({"campaign_id": campaign_id})


@router.post("/test-send")
def test_send(payload: dict = Body(...)):
    channel = payload.get("channel") or "email"
    to = (payload.get("to") or "").strip()
    if not to:
        return JSONResponse({"error": "Enter a test email or WhatsApp number."}, status_code=400)
    cfg = _get_cfg()
    cfg["from_name"] = cfg.get("from_name") or get_setting("hotel_name", "") or ""
    hotel = _hotel_info()
    profiles = _build_profiles()
    cid = payload.get("customer_id")
    p = profiles.get(int(cid)) if str(cid or "").isdigit() else None
    values = _merge_values(p, hotel) if p else {**hotel, "name": "Test Guest", "first_name": "Test"}
    subject = "[TEST] " + _render(payload.get("subject") or "", values)
    body = _render(payload.get("body") or "", values)

    try:
        if channel == "email":
            if not _email_ready(cfg):
                return JSONResponse({"error": "Email sender is not configured."}, status_code=400)
            s = _SmtpSender(cfg)
            try:
                s.send(to, subject, body)
            finally:
                s.close()
            return JSONResponse({"ok": True, "message": f"Test email sent to {to}"})
        phone = _norm_phone(to, cfg.get("wa_default_country_code", ""))
        if _effective_wa_mode(cfg) == "api":
            _wa_api_send(cfg, phone, body, values)
            return JSONResponse({"ok": True, "message": f"Test WhatsApp sent to +{phone}"})
        return JSONResponse({"ok": True, "link": _wa_link(phone, body),
                             "message": "Opening WhatsApp with the test message..."})
    except Exception as e:
        return JSONResponse({"error": f"Send failed: {e}"}, status_code=400)


# ---------------------------------------------------------------------------
# Sender settings API
# ---------------------------------------------------------------------------
def _public_cfg(cfg):
    out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    out["has_smtp_password"] = bool(cfg.get("smtp_password"))
    out["has_wa_access_token"] = bool(cfg.get("wa_access_token"))
    out["email_ready"] = _email_ready(cfg)
    out["wa_api_ready"] = _wa_api_ready(cfg)
    out["wa_effective_mode"] = _effective_wa_mode(cfg)
    return out


@router.get("/sender-settings")
def get_sender_settings():
    return JSONResponse(_public_cfg(_get_cfg()))


@router.post("/sender-settings")
def save_sender_settings(payload: dict = Body(...)):
    with get_db() as db:
        for key in SETTING_DEFAULTS:
            if key not in payload:
                continue
            val = "" if payload[key] is None else str(payload[key]).strip()
            if key in SECRET_KEYS and val == "":
                continue  # blank = keep existing secret
            db.execute("REPLACE INTO campaign_settings (key, value) VALUES (?, ?)", (key, val))
        for key in SECRET_KEYS:
            if payload.get(f"clear_{key}"):
                db.execute("REPLACE INTO campaign_settings (key, value) VALUES (?, '')", (key,))
    return JSONResponse({"ok": True, "settings": _public_cfg(_get_cfg())})


# ---------------------------------------------------------------------------
# Recipient actions
# ---------------------------------------------------------------------------
@router.post("/recipients/{rid}/status")
def set_recipient_status(rid: int, payload: dict = Body(...)):
    status = payload.get("status")
    if status not in ("sent", "skipped", "pending", "failed"):
        return JSONResponse({"error": "Invalid status"}, status_code=400)
    with get_db() as db:
        db.execute("SELECT campaign_id FROM campaign_recipients WHERE id=?", (rid,))
        row = db.fetchone()
        if not row:
            return JSONResponse({"error": "Not found"}, status_code=404)
        db.execute("UPDATE campaign_recipients SET status=?, sent_at=?, error=NULL WHERE id=?",
                   (status, datetime.now().isoformat() if status != "pending" else None, rid))
        cid = row["campaign_id"]
    _finalize_status(cid)
    with get_db() as db:
        counts = _campaign_counts(db, cid)
    return JSONResponse({"ok": True, "counts": counts})


@router.post("/recipients/{rid}/opt-out")
def opt_out_recipient(rid: int):
    with get_db() as db:
        db.execute("SELECT customer_id FROM campaign_recipients WHERE id=?", (rid,))
        row = db.fetchone()
        if not row or not row["customer_id"]:
            return JSONResponse({"error": "Not found"}, status_code=404)
        db.execute("UPDATE customers SET marketing_opt_out=1 WHERE id=?", (row["customer_id"],))
    return JSONResponse({"ok": True})


@router.post("/customers/{customer_id}/opt-in")
def opt_in_customer(customer_id: int):
    with get_db() as db:
        db.execute("UPDATE customers SET marketing_opt_out=0 WHERE id=?", (customer_id,))
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Campaign detail / actions
# ---------------------------------------------------------------------------
def _load_campaign(cid):
    with get_db() as db:
        db.execute("SELECT * FROM campaigns WHERE id=?", (cid,))
        c = db.fetchone()
        if not c:
            return None, None, None
        c = dict(c)
        db.execute("SELECT * FROM campaign_recipients WHERE campaign_id=? ORDER BY id", (cid,))
        recipients = [dict(r) for r in db.fetchall()]
        counts = _campaign_counts(db, cid)
    return c, recipients, counts


@router.get("/{cid}/status")
def campaign_status(cid: int):
    c, recipients, counts = _load_campaign(cid)
    if not c:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({
        "status": c["status"],
        "counts": counts,
        "recipients": [{"id": r["id"], "status": r["status"], "error": r["error"],
                        "sent_at": r["sent_at"]} for r in recipients],
    })


@router.get("/{cid}/json")
def campaign_json(cid: int):
    c, recipients, _ = _load_campaign(cid)
    if not c:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        filters = json.loads(c.get("filters_json") or "{}")
    except Exception:
        filters = {}
    ids = sorted({r["customer_id"] for r in recipients if r["customer_id"]})
    return JSONResponse({"name": c["name"], "channel": c["channel"], "subject": c["subject"],
                         "body": c["body"], "filters": filters, "customer_ids": ids})


@router.post("/{cid}/retry-failed")
def retry_failed(cid: int):
    with get_db() as db:
        db.execute("UPDATE campaign_recipients SET status='pending', error=NULL "
                   "WHERE campaign_id=? AND status='failed'", (cid,))
        db.execute("UPDATE campaigns SET status='sending', completed_at=NULL WHERE id=?", (cid,))
    _start_worker(cid)
    return JSONResponse({"ok": True})


@router.post("/{cid}/delete")
def delete_campaign(cid: int):
    with get_db() as db:
        db.execute("DELETE FROM campaign_recipients WHERE campaign_id=?", (cid,))
        db.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    return JSONResponse({"ok": True})


@router.get("/{cid}", response_class=HTMLResponse)
def campaign_detail(cid: int, request: Request):
    c, recipients, counts = _load_campaign(cid)
    if not c:
        return RedirectResponse("/campaigns", status_code=303)
    for r in recipients:
        r["wa_link"] = (_wa_link(r["address"], r["message"])
                        if r["channel"] == "whatsapp" and r["address"] else "")
        r["sent_fmt"] = _parse_dt(r["sent_at"]).strftime("%d %b, %I:%M %p") if r.get("sent_at") else ""
    c["created_fmt"] = _parse_dt(c["created_at"]).strftime("%d %b %Y, %I:%M %p") if c.get("created_at") else ""
    manual_queue = [r for r in recipients
                    if r["channel"] == "whatsapp" and c.get("wa_mode") != "api" and r["address"]]
    return templates.TemplateResponse("campaign-detail.html", {
        "request": request, "c": c, "recipients": recipients, "counts": counts,
        "manual_queue": manual_queue,
    })
