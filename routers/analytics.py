# routers/analytics.py
from fastapi import APIRouter, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from dependencies import get_db
from datetime import datetime, timedelta
import json
from utils.cache import cached
from routers.settings import get_setting

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now

@router.get("/")
def analytics_dashboard(request: Request):
    from routers.settings import get_setting
    currency = get_setting("currency_symbol", "$")
    return templates.TemplateResponse("analytics.html", {"request": request, "currency": currency})

@router.get("/data")
def analytics_data(
    range: str = Query("7days"),
    start_date: str | None = None,
    end_date: str | None = None
):
    with get_db() as db:
        # Determine time filter
        today = datetime.today().date()
        if range == "today":
            start, end = today, today
        elif range == "7days":
            start, end = today - timedelta(days=7), today
        elif range == "30days":
            start, end = today - timedelta(days=30), today
        elif range == "90days":
            start, end = today - timedelta(days=90), today
        elif range == "month":
            start, end = today.replace(day=1), today
        elif range == "12months":
            start, end = today - timedelta(days=365), today
        elif range == "24months":
            start, end = today - timedelta(days=730), today
        elif range == "custom" and start_date and end_date:
            try:
                start = datetime.strptime(start_date, "%Y-%m-%d").date()
                end = datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                start, end = today - timedelta(days=7), today
        else:
            start, end = today - timedelta(days=7), today  # default


        # 1. Total rooms
        db.execute("SELECT COUNT(*) AS c FROM rooms")
        total_rooms = (db.fetchone() or {"c": 0})["c"]

        # 2. Occupied & available rooms
        db.execute("SELECT COUNT(*) AS c FROM rooms WHERE status = 'Occupied'")
        occupied = (db.fetchone() or {"c": 0})["c"]
        db.execute("SELECT COUNT(*) AS c FROM rooms WHERE status = 'Available'")
        available = (db.fetchone() or {"c": 0})["c"]

        # 3. Revenue (filtered by date range)
        db.execute("""
            SELECT SUM(total_amount) AS s
            FROM bookings
            WHERE DATE(check_in) BETWEEN ? AND ?
        """, (start, end))
        revenue = (db.fetchone() or {"s": 0})["s"] or 0.0
        
        # 4. Rooms by Type count (Pie chart data)
        db.execute("SELECT room_type, COUNT(*) as count FROM rooms GROUP BY room_type")
        rooms_by_type = [{"name": r["room_type"], "value": r["count"]} for r in db.fetchall()]
        
        # 5. Top 5 Customers by Spending
        db.execute("""
            SELECT c.name, SUM(b.total_amount) as total_spent
            FROM bookings b
            JOIN customers c ON b.customer_id = c.id
            GROUP BY c.id
            ORDER BY total_spent DESC
            LIMIT 5
        """)
        top_customers = [{"name": r["name"], "total": r["total_spent"]} for r in db.fetchall()]

        # 6. Daily Stats (for Line Chart) - Aggregate by day
        date_list = []
        occupancy_data = []
        revenue_data = []
        
        current_date = start
        while current_date <= end:
            d_str = current_date.strftime("%Y-%m-%d")
            date_list.append(d_str)
            
            # Simple daily revenue query
            db.execute("""
                SELECT SUM(total_amount) as s 
                FROM bookings 
                WHERE DATE(check_in) = ?
            """, (d_str,))
            day_rev = (db.fetchone() or {"s": 0})["s"] or 0.0
            revenue_data.append(day_rev)
            
            # Simple daily occupancy query (Mock: In real life, calculate overlapping dates)
            # For this simple app, we count bookings strictly checking in on this date or active?
            # Let's count active bookings for occupancy
            db.execute("""
                SELECT COUNT(*) as c 
                FROM bookings 
                WHERE DATE(check_in) <= ? AND DATE(expected_check_out) > ?
            """, (d_str, d_str))
            day_occ = (db.fetchone() or {"c": 0})["c"]
            # Convert to percentage
            day_occ_pct = round((day_occ / total_rooms * 100), 1) if total_rooms > 0 else 0
            occupancy_data.append(day_occ_pct)
            
            current_date += timedelta(days=1)

        # 7. Payment Methods Breakdown (for Doughnut chart)
        db.execute("""
            SELECT payment_method, COUNT(*) as count, SUM(amount) as total
            FROM payments
            WHERE payment_method IS NOT NULL AND payment_method != ''
            GROUP BY payment_method
            ORDER BY count DESC
        """)
        payment_methods = [
            {"method": r["payment_method"], "count": r["count"], "total": float(r["total"] or 0)}
            for r in db.fetchall()
        ]

    return JSONResponse({
        "total_rooms": total_rooms,
        "occupied": occupied,
        "available": available,
        "revenue": float(revenue),
        "start_date": str(start),
        "end_date": str(end),
        "rooms_by_type": rooms_by_type,
        "top_customers": top_customers,
        "labels": date_list,
        "revenue_trend": revenue_data,
        "daily_occupancy": occupancy_data,
        "daily_revenue": revenue_data,
        "payment_methods": payment_methods
    })

@router.get("/details")
def analytics_details(date: str):
    """
    Returns specific booking details for a clicked date.
    Deep dive feature.
    """
    with get_db() as db:
        db.execute("""
            SELECT b.id, b.room_number, c.name as customer, b.status, b.total_amount
            FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id
            WHERE DATE(b.check_in) = ?
        """, (date,))
        rows = db.fetchall()
        
    return JSONResponse({
        "date": date,
        "bookings": [
            {
                "id": r["id"],
                "room": r["room_number"],
                "customer": r["customer"],
                "status": r["status"],
                "amount": r["total_amount"]
            } for r in rows
        ]
    })

@router.get("/advanced")
@cached(ttl_seconds=300)  # Cache for 5 minutes
def analytics_advanced():
    """
    Returns advanced analytics: booking velocity, RevPAR, avg stay length, etc.
    """
    with get_db() as db:
        # 1. Booking Velocity (last 30 days, grouped by week)
        db.execute("""
            SELECT DATE(check_in) as date, COUNT(*) as count
            FROM bookings
            WHERE DATE(check_in) >= date('now', '-30 days')
            GROUP BY DATE(check_in)
            ORDER BY date
        """)
        daily_bookings = db.fetchall()
        
        # 2. Average Length of Stay
        db.execute("""
            SELECT AVG(num_nights) as avg_los
            FROM bookings
            WHERE status IN ('Checked-out', 'Active')
        """)
        avg_los = (db.fetchone() or {"avg_los": 0})["avg_los"] or 0
        
        # 3. Revenue Per Available Room (RevPAR)
        db.execute("SELECT COUNT(*) as total FROM rooms")
        total_rooms = (db.fetchone() or {"total": 0})["total"]
        
        db.execute("""
            SELECT SUM(total_amount) as revenue
            FROM bookings
            WHERE DATE(check_in) >= date('now', '-30 days')
        """)
        revenue_30d = (db.fetchone() or {"revenue": 0})["revenue"] or 0
        revpar = (revenue_30d / (total_rooms * 30)) if total_rooms > 0 else 0
        
        # 4. Revenue by Room Type
        db.execute("""
            SELECT r.room_type, SUM(b.total_amount) as revenue
            FROM bookings b
            JOIN rooms r ON b.room_number = r.room_number
            WHERE b.status IN ('Checked-out', 'Active', 'Booked')
            GROUP BY r.room_type
        """)
        revenue_by_type = [{"type": row["room_type"], "revenue": row["revenue"]} for row in db.fetchall()]
        
    return JSONResponse({
        "booking_velocity": [
            {"date": row["date"], "count": row["count"]}
            for row in daily_bookings
        ],
        "avg_length_of_stay": round(avg_los, 1),
        "revpar": round(revpar, 2),
        "revenue_by_room_type": revenue_by_type
    })


@router.get("/comparison")
def analytics_comparison():
    """
    Returns Year-over-Year and Month-over-Month comparison metrics.
    Compares current year-to-date vs same period last year.
    """
    today = datetime.today().date()
    current_year = today.year
    previous_year = current_year - 1
    
    # YTD dates: Jan 1 to today
    ytd_start_this_year = f"{current_year}-01-01"
    ytd_end_this_year = today.strftime("%Y-%m-%d")
    
    ytd_start_prev_year = f"{previous_year}-01-01"
    # Same day-of-year in previous year
    ytd_end_prev_year = f"{previous_year}-{today.strftime('%m-%d')}"
    
    # Previous month dates
    if today.month == 1:
        prev_month = 12
        prev_month_year = previous_year
    else:
        prev_month = today.month - 1
        prev_month_year = current_year
    
    prev_month_start = f"{prev_month_year}-{str(prev_month).zfill(2)}-01"
    # Last day of previous month
    if prev_month == 12:
        prev_month_end = f"{prev_month_year}-12-31"
    else:
        import calendar
        last_day = calendar.monthrange(prev_month_year, prev_month)[1]
        prev_month_end = f"{prev_month_year}-{str(prev_month).zfill(2)}-{str(last_day).zfill(2)}"
    
    # Current month dates
    curr_month_start = f"{current_year}-{str(today.month).zfill(2)}-01"
    curr_month_end = today.strftime("%Y-%m-%d")
    
    with get_db() as db:
        # --- YTD This Year ---
        db.execute("""
            SELECT COUNT(*) as count, SUM(total_amount) as revenue
            FROM bookings
            WHERE DATE(check_in) BETWEEN ? AND ?
        """, (ytd_start_this_year, ytd_end_this_year))
        ytd_this = db.fetchone()
        ytd_this_bookings = ytd_this["count"] or 0
        ytd_this_revenue = ytd_this["revenue"] or 0
        
        # --- YTD Previous Year ---
        db.execute("""
            SELECT COUNT(*) as count, SUM(total_amount) as revenue
            FROM bookings
            WHERE DATE(check_in) BETWEEN ? AND ?
        """, (ytd_start_prev_year, ytd_end_prev_year))
        ytd_prev = db.fetchone()
        ytd_prev_bookings = ytd_prev["count"] or 0
        ytd_prev_revenue = ytd_prev["revenue"] or 0
        
        # --- Previous Month ---
        db.execute("""
            SELECT COUNT(*) as count, SUM(total_amount) as revenue
            FROM bookings
            WHERE DATE(check_in) BETWEEN ? AND ?
        """, (prev_month_start, prev_month_end))
        prev_month_data = db.fetchone()
        prev_month_bookings = prev_month_data["count"] or 0
        prev_month_revenue = prev_month_data["revenue"] or 0
        
        # --- Current Month (partial) ---
        db.execute("""
            SELECT COUNT(*) as count, SUM(total_amount) as revenue
            FROM bookings
            WHERE DATE(check_in) BETWEEN ? AND ?
        """, (curr_month_start, curr_month_end))
        curr_month_data = db.fetchone()
        curr_month_bookings = curr_month_data["count"] or 0
        curr_month_revenue = curr_month_data["revenue"] or 0

        # --- Group vs Individual breakdowns for YTD ---
        db.execute("""
            SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
            FROM bookings WHERE group_id IS NOT NULL AND DATE(check_in) BETWEEN ? AND ?
        """, (ytd_start_this_year, ytd_end_this_year))
        ytd_group = db.fetchone()
        db.execute("""
            SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
            FROM bookings WHERE group_id IS NULL AND DATE(check_in) BETWEEN ? AND ?
        """, (ytd_start_this_year, ytd_end_this_year))
        ytd_individual = db.fetchone()

        db.execute("""
            SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
            FROM bookings WHERE group_id IS NOT NULL AND DATE(check_in) BETWEEN ? AND ?
        """, (ytd_start_prev_year, ytd_end_prev_year))
        ytd_group_prev = db.fetchone()
        db.execute("""
            SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
            FROM bookings WHERE group_id IS NULL AND DATE(check_in) BETWEEN ? AND ?
        """, (ytd_start_prev_year, ytd_end_prev_year))
        ytd_individual_prev = db.fetchone()

        # --- Group vs Individual breakdowns for MoM ---
        db.execute("""
            SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
            FROM bookings WHERE group_id IS NOT NULL AND DATE(check_in) BETWEEN ? AND ?
        """, (curr_month_start, curr_month_end))
        mom_group = db.fetchone()
        db.execute("""
            SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
            FROM bookings WHERE group_id IS NULL AND DATE(check_in) BETWEEN ? AND ?
        """, (curr_month_start, curr_month_end))
        mom_individual = db.fetchone()

        db.execute("""
            SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
            FROM bookings WHERE group_id IS NOT NULL AND DATE(check_in) BETWEEN ? AND ?
        """, (prev_month_start, prev_month_end))
        mom_group_prev = db.fetchone()
        db.execute("""
            SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
            FROM bookings WHERE group_id IS NULL AND DATE(check_in) BETWEEN ? AND ?
        """, (prev_month_start, prev_month_end))
        mom_individual_prev = db.fetchone()

    # Calculate growth percentages
    ytd_revenue_growth = ((ytd_this_revenue - ytd_prev_revenue) / ytd_prev_revenue * 100) if ytd_prev_revenue > 0 else 0
    ytd_bookings_growth = ((ytd_this_bookings - ytd_prev_bookings) / ytd_prev_bookings * 100) if ytd_prev_bookings > 0 else 0
    mom_revenue_growth = ((curr_month_revenue - prev_month_revenue) / prev_month_revenue * 100) if prev_month_revenue > 0 else 0
    
    return JSONResponse({
        "ytd": {
            "current_year": current_year,
            "previous_year": previous_year,
            "current": {
                "bookings": ytd_this_bookings,
                "revenue": round(ytd_this_revenue, 2),
                "period": f"Jan 1 - {today.strftime('%b %d')}, {current_year}"
            },
            "previous": {
                "bookings": ytd_prev_bookings,
                "revenue": round(ytd_prev_revenue, 2),
                "period": f"Jan 1 - {today.strftime('%b %d')}, {previous_year}"
            },
            "revenue_growth": round(ytd_revenue_growth, 1),
            "bookings_growth": round(ytd_bookings_growth, 1),
            "group": {
                "current": {"bookings": ytd_group["c"], "revenue": round(float(ytd_group["rev"]), 2)},
                "previous": {"bookings": ytd_group_prev["c"], "revenue": round(float(ytd_group_prev["rev"]), 2)}
            },
            "individual": {
                "current": {"bookings": ytd_individual["c"], "revenue": round(float(ytd_individual["rev"]), 2)},
                "previous": {"bookings": ytd_individual_prev["c"], "revenue": round(float(ytd_individual_prev["rev"]), 2)}
            }
        },
        "mom": {
            "current_month": today.strftime("%B %Y"),
            "previous_month": f"{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][prev_month-1]} {prev_month_year}",
            "current": {
                "bookings": curr_month_bookings,
                "revenue": round(curr_month_revenue, 2)
            },
            "previous": {
                "bookings": prev_month_bookings,
                "revenue": round(prev_month_revenue, 2)
            },
            "revenue_growth": round(mom_revenue_growth, 1),
            "group": {
                "current": {"bookings": mom_group["c"], "revenue": round(float(mom_group["rev"]), 2)},
                "previous": {"bookings": mom_group_prev["c"], "revenue": round(float(mom_group_prev["rev"]), 2)}
            },
            "individual": {
                "current": {"bookings": mom_individual["c"], "revenue": round(float(mom_individual["rev"]), 2)},
                "previous": {"bookings": mom_individual_prev["c"], "revenue": round(float(mom_individual_prev["rev"]), 2)}
            }
        }
    })



@router.get("/booking-types")
def analytics_booking_types():
    """
    Returns group vs individual booking breakdown,
    plus last 6 months month-by-month split for comparison chart.
    """
    today = datetime.today().date()
    with get_db() as db:
        # Overall group vs individual
        db.execute("SELECT COUNT(*) as c FROM bookings WHERE group_id IS NOT NULL")
        group_total = (db.fetchone() or {"c": 0})["c"]
        db.execute("SELECT COUNT(*) as c FROM bookings WHERE group_id IS NULL")
        individual_total = (db.fetchone() or {"c": 0})["c"]

        # Per-month last 6 months
        monthly_labels = []
        monthly_group = []
        monthly_individual = []
        monthly_revenue_group = []
        monthly_revenue_individual = []

        for i in range(5, -1, -1):
            # Go back i months from current month
            import calendar
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            last_day = calendar.monthrange(year, month)[1]
            month_start = f"{year}-{str(month).zfill(2)}-01"
            month_end = f"{year}-{str(month).zfill(2)}-{str(last_day).zfill(2)}"
            label = f"{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][month-1]} {year}"
            monthly_labels.append(label)

            db.execute("""
                SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
                FROM bookings
                WHERE group_id IS NOT NULL AND DATE(check_in) BETWEEN ? AND ?
            """, (month_start, month_end))
            r = db.fetchone()
            monthly_group.append(r["c"])
            monthly_revenue_group.append(round(float(r["rev"]), 2))

            db.execute("""
                SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0) as rev
                FROM bookings
                WHERE group_id IS NULL AND DATE(check_in) BETWEEN ? AND ?
            """, (month_start, month_end))
            r = db.fetchone()
            monthly_individual.append(r["c"])
            monthly_revenue_individual.append(round(float(r["rev"]), 2))

    return JSONResponse({
        "group_total": group_total,
        "individual_total": individual_total,
        "monthly_labels": monthly_labels,
        "monthly_group": monthly_group,
        "monthly_individual": monthly_individual,
        "monthly_revenue_group": monthly_revenue_group,
        "monthly_revenue_individual": monthly_revenue_individual,
    })


@router.get("/daily-report", response_class=HTMLResponse)
def get_daily_report(
    request: Request,
    period_type: str = "daily",
    date: str = None,
    month: str = None,
    start_date: str = None,
    end_date: str = None,
    print_mode: bool = False
):
    now = datetime.now()
    
    # 1) Resolve date ranges
    if not date:
        date = now.strftime("%Y-%m-%d")
    if not month:
        month = now.strftime("%Y-%m")
    if not start_date:
        start_date = now.strftime("%Y-%m-%d")
    if not end_date:
        end_date = now.strftime("%Y-%m-%d")
        
    start_str = start_date
    end_str = end_date
    
    if period_type == "daily":
        start_str = date
        end_str = date
    elif period_type == "weekly":
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            dt = now
        # Monday is 0, Sunday is 6
        monday = dt - timedelta(days=dt.weekday())
        start_str = monday.strftime("%Y-%m-%d")
        end_str = (monday + timedelta(days=6)).strftime("%Y-%m-%d")
    elif period_type == "monthly":
        try:
            y, m_val = map(int, month.split("-"))
        except Exception:
            y, m_val = now.year, now.month
        import calendar
        last_day = calendar.monthrange(y, m_val)[1]
        start_str = f"{y}-{str(m_val).zfill(2)}-01"
        end_str = f"{y}-{str(m_val).zfill(2)}-{str(last_day).zfill(2)}"
        
    # 2) Fetch report data
    with get_db() as db:
        db.execute("""
            SELECT r.room_number, r.room_type, r.price_per_night,
                   b.id as booking_id, c.name as guest_name, b.pax,
                   b.check_in, b.expected_check_out, b.num_nights,
                   b.room_rate, ref.name as reference_name, b.status
            FROM bookings b
            JOIN rooms r ON r.room_number = b.room_number
            LEFT JOIN customers c ON b.customer_id = c.id
            LEFT JOIN customers ref ON b.reference_id = ref.id
            WHERE DATE(b.check_in) >= DATE(?) AND DATE(b.check_in) <= DATE(?)
                AND b.status IN ('Booked', 'Reserved', 'Checked-out')
            ORDER BY CAST(r.room_number AS INTEGER), b.check_in ASC
        """, (start_str, end_str))
        rows = db.fetchall()
        
    # 3) Process results and calculate summary metrics
    report_rows = []
    total_occupied = 0
    total_pax = 0
    grand_total_rent = 0.0
    
    for r in rows:
        row_dict = dict(r)
        if row_dict["booking_id"]:
            total_occupied += 1
            total_pax += int(row_dict["pax"] or 1)
            
            # Rent is room_rate * num_nights
            rent = float(row_dict["room_rate"] or 0) * int(row_dict["num_nights"] or 1)
            row_dict["rent"] = rent
            grand_total_rent += rent
            
            # Format dates to DD/MM/YY
            try:
                in_dt = datetime.fromisoformat(row_dict["check_in"])
                row_dict["check_in_formatted"] = in_dt.strftime("%d/%m/%y")
            except:
                row_dict["check_in_formatted"] = row_dict["check_in"]
                
            try:
                out_dt = datetime.fromisoformat(row_dict["expected_check_out"])
                row_dict["check_out_formatted"] = out_dt.strftime("%d/%m/%y")
            except:
                row_dict["check_out_formatted"] = row_dict["expected_check_out"]
        else:
            row_dict["rent"] = None
            row_dict["check_in_formatted"] = ""
            row_dict["check_out_formatted"] = ""
            
        report_rows.append(row_dict)
        
    currency_symbol = get_setting("currency_symbol", "$")
    
    return templates.TemplateResponse(
        "daily-report.html",
        {
            "request": request,
            "report_rows": report_rows,
            "period_type": period_type,
            "date": date,
            "month": month,
            "start_date": start_date,
            "end_date": end_date,
            "start_str": start_str,
            "end_str": end_str,
            "total_occupied": total_occupied,
            "total_pax": total_pax,
            "grand_total_rent": grand_total_rent,
            "currency": currency_symbol,
            "print_mode": print_mode
        }
    )


@router.get("/daily-report/print", response_class=HTMLResponse)
def print_daily_report(
    request: Request,
    period_type: str = "daily",
    date: str = None,
    month: str = None,
    start_date: str = None,
    end_date: str = None
):
    return get_daily_report(request, period_type, date, month, start_date, end_date, print_mode=True)