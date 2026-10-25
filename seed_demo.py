"""
HavenDesk — Clean Demo Database Initializer & Seeder.
Creates hotel_management.db from schema.sql and populates realistic synthetic hotel data.
Default login: admin / admin123
"""
import os
import json
import sqlite3
import bcrypt
from datetime import datetime, timedelta

DB_PATH = os.getenv("SQLITE_DB_PATH", "hotel_management.db")

def init_and_seed():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    with open("schema.sql", "r", encoding="utf-8") as f:
        cur.executescript(f.read())

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 1. Admin User (admin / admin123)
    pw_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt())
    cur.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        ("admin", pw_hash, "admin", now.isoformat())
    )

    # 2. Settings
    settings = [
        ("hotel_name", "HavenDesk Grand Hotel"),
        ("hotel_email", "concierge@havendesk.example"),
        ("hotel_phone", "+880 1700-000000"),
        ("hotel_address", "42 Emerald Avenue, Gulshan-2, Dhaka"),
        ("currency_symbol", "৳"),
        ("checkout_time", "12:00"),
        ("tax_percent", "10"),
        ("late_checkin_penalty_percent", "0"),
    ]
    for k, v in settings:
        cur.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (k, v, now.isoformat()))

    # 3. Rooms
    rooms = [
        ("101", "Deluxe Single", 3500, "Occupied", "Clean"),
        ("102", "Deluxe Single", 3500, "Available", "Clean"),
        ("103", "Emerald Double", 5200, "Occupied", "Clean"),
        ("201", "Emerald Double", 5200, "Occupied", "Clean"),
        ("202", "Haven Executive Suite", 8500, "Available", "Clean"),
        ("203", "Haven Executive Suite", 8500, "Reserved", "Clean"),
        ("301", "Royal Penthouse", 12500, "Occupied", "Clean"),
        ("302", "Family Quad", 7000, "Available", "Dirty"),
        ("303", "Family Quad", 7000, "Maintenance", "Clean"),
    ]
    for rn, rt, price, st, hk in rooms:
        cur.execute(
            "INSERT INTO rooms (room_number, room_type, price_per_night, status, housekeeping_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rn, rt, price, st, hk, now.isoformat(), now.isoformat())
        )

    # 4. Customers
    customers = [
        ("Aarav Chowdhury", "aarav.c@example.com", "01711223344", "NID-8829101", "Banani, Dhaka", "VIP", 420),
        ("Nusrat Jahan", "nusrat.j@example.com", "01822334455", "PASS-B99821", "Dhanmondi, Dhaka", "Regular", 180),
        ("Tariqul Islam", "tariq.islam@example.com", "01933445566", "NID-5543219", "Uttara, Dhaka", "Corporate", 310),
        ("Samira Karim", "samira.k@example.com", "01644556677", "NID-3319482", "Chittagong", "VIP", 510),
        ("Rafiq Ahmed", "rafiq.a@example.com", "01755667788", "NID-7748291", "Sylhet", "Regular", 90),
        ("Farhana Rahman", "farhana.r@example.com", "01566778899", "PASS-A44210", "Gulshan, Dhaka", "Corporate", 260),
    ]
    cust_ids = []
    for name, email, phone, idp, addr, ctype, pts in customers:
        cur.execute(
            "INSERT INTO customers (name, email, phone, id_proof, address, customer_type, loyalty_points, marketing_opt_out, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (name, email, phone, idp, addr, ctype, pts, (now - timedelta(days=60)).isoformat(), now.isoformat())
        )
        cust_ids.append(cur.lastrowid)

    # 5. Services Catalog
    catalog = [
        ("Airport Limousine Pickup", 2500, "Transport", "High", "Private AC sedan pickup from airport"),
        ("Complimentary Breakfast Buffet", 850, "Food & Beverage", "Normal", "Continental & local buffet"),
        ("Express Laundry Service", 600, "Housekeeping", "Normal", "Same-day wash & fold"),
        ("Spa & Wellness Package", 3200, "Wellness", "High", "60-min aromatherapy session"),
    ]
    for name, price, cat, prio, desc in catalog:
        cur.execute(
            "INSERT INTO services_catalog (name, price, category, priority, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, price, cat, prio, desc, now.isoformat())
        )

    # 6. Bookings (Active today + recent history)
    extras_sample = json.dumps([{"description": "Airport Limousine Pickup", "amount": 2500}])
    bookings = [
        # Checked-in today (shows on Daily Report immediately)
        (cust_ids[0], "101", now.strftime("%Y-%m-%dT12:00:00"), (now + timedelta(days=2)).strftime("%Y-%m-%dT12:00:00"), None, 3500, 2, "[]", 7000, 3500, 3500, "Partial", None, "Booked", "Card", 2, cust_ids[3]),
        (cust_ids[1], "103", now.strftime("%Y-%m-%dT14:00:00"), (now + timedelta(days=3)).strftime("%Y-%m-%dT12:00:00"), None, 5200, 3, extras_sample, 18100, 10000, 10000, "Partial", None, "Booked", "bKash", 2, cust_ids[0]),
        (cust_ids[2], "201", now.strftime("%Y-%m-%dT15:30:00"), (now + timedelta(days=1)).strftime("%Y-%m-%dT12:00:00"), None, 5200, 1, "[]", 5200, 5200, 5200, "Paid", None, "Booked", "Cash", 1, None),
        (cust_ids[3], "301", now.strftime("%Y-%m-%dT16:00:00"), (now + timedelta(days=4)).strftime("%Y-%m-%dT12:00:00"), None, 12500, 4, extras_sample, 52500, 25000, 25000, "Partial", None, "Booked", "Card", 3, None),
        # Upcoming reservation
        (cust_ids[4], "203", (now + timedelta(days=2)).strftime("%Y-%m-%dT12:00:00"), (now + timedelta(days=5)).strftime("%Y-%m-%dT12:00:00"), None, 8500, 3, "[]", 25500, 5000, 5000, "Partial", None, "Reserved", "bKash", 2, cust_ids[0]),
        # Past checked-out stays
        (cust_ids[0], "202", (now - timedelta(days=14)).strftime("%Y-%m-%dT12:00:00"), (now - timedelta(days=11)).strftime("%Y-%m-%dT12:00:00"), (now - timedelta(days=11)).strftime("%Y-%m-%dT11:30:00"), 8500, 3, extras_sample, 28000, 28000, 28000, "Paid", None, "Checked-out", "Card", 2, None),
        (cust_ids[5], "102", (now - timedelta(days=7)).strftime("%Y-%m-%dT12:00:00"), (now - timedelta(days=5)).strftime("%Y-%m-%dT12:00:00"), (now - timedelta(days=5)).strftime("%Y-%m-%dT11:00:00"), 3500, 2, "[]", 7000, 7000, 7000, "Paid", None, "Checked-out", "Cash", 1, cust_ids[2]),
    ]
    for b in bookings:
        cur.execute("""
            INSERT INTO bookings (
                customer_id, room_number, check_in, expected_check_out, actual_check_out,
                room_rate, num_nights, extra_charges, total_amount, advance_amount, amount_paid,
                payment_status, group_id, status, payment_method, pax, reference_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, b + (now.isoformat(), now.isoformat()))
        bid = cur.lastrowid
        if b[10] > 0:
            cur.execute(
                "INSERT INTO payments (booking_id, amount, payment_method, payment_type, notes, created_at) VALUES (?, ?, ?, 'advance', 'Demo payment', ?)",
                (bid, b[10], b[14], now.isoformat())
            )

    # 7. Inventory
    inv = [
        ("Egyptian Cotton Bath Towels", "Linens", 85, 30, "pcs", 450, "Dhaka Textile Mills"),
        ("Organic Lemongrass Soap Bars", "Toiletries", 140, 50, "pcs", 65, "GreenCare BD"),
        ("Mineral Water Bottles (500ml)", "Beverages", 220, 100, "bottles", 20, "FreshSip Ltd"),
        ("Luxury Slippers (Pairs)", "Amenities", 18, 25, "pairs", 120, "ComfortStep"),
    ]
    for name, cat, stock, par, unit, price, sup in inv:
        cur.execute(
            "INSERT INTO inventory (name, category, current_stock, par_level, unit, unit_price, supplier, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, cat, stock, par, unit, price, sup, now.isoformat())
        )
        cur.execute(
            "INSERT INTO inventory_items (item_name, quantity, unit, low_stock_threshold, supplier, last_updated) VALUES (?, ?, ?, ?, ?, ?)",
            (name, stock, unit, par, sup, now.isoformat())
        )

    # 8. Financials
    txs = [
        ("income", 28000, "Room Revenue", "Suite 202 Checkout Settlement", (now - timedelta(days=11)).strftime("%Y-%m-%d"), "admin", 6),
        ("income", 43700, "Advance Payments", "Front-desk check-in advances", today_str, "admin", 1),
        ("expense", 12500, "Utilities", "Monthly commercial electricity & generator fuel", (now - timedelta(days=3)).strftime("%Y-%m-%d"), "admin", None),
        ("expense", 6800, "Supplies", "Restocked organic toiletries & mineral water", (now - timedelta(days=1)).strftime("%Y-%m-%d"), "admin", None),
    ]
    for t in txs:
        cur.execute(
            "INSERT INTO financial_transactions (type, amount, category, description, date, entered_by, related_booking_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            t
        )

    # 9. Sample Campaign History
    cur.execute(
        "INSERT INTO campaigns (name, channel, wa_mode, subject, body, filters_json, status, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Autumn VIP Appreciation Offer",
            "smart",
            "manual",
            "A special thank-you from HavenDesk, {first_name}!",
            "Dear {name},\n\nThank you for your loyalty ({total_stays} stays with us!). Enjoy 20% off your next booking at {hotel_name}.\n\nWarm regards,\nHavenDesk Concierge",
            json.dumps({"match": "all", "rules": [{"field": "customer_type", "op": "any_of", "value": ["VIP"]}]}),
            "completed",
            (now - timedelta(days=2)).isoformat(),
            (now - timedelta(days=2)).isoformat(),
        )
    )
    cid = cur.lastrowid
    for idx in (0, 3):
        c = customers[idx]
        cur.execute(
            "INSERT INTO campaign_recipients (campaign_id, customer_id, name, channel, address, subject, message, status, sent_at) VALUES (?, ?, ?, 'email', ?, ?, ?, 'sent', ?)",
            (cid, cust_ids[idx], c[0], c[1], f"A special thank-you from HavenDesk, {c[0].split()[0]}!", f"Dear {c[0]}, Thank you for your loyalty!", (now - timedelta(days=2)).isoformat())
        )

    conn.commit()
    conn.close()
    print(f"✅ Seeded clean demo database at {DB_PATH}")

if __name__ == "__main__":
    init_and_seed()
