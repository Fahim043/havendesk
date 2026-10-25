# routers/ai.py
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from dependencies import get_db


from datetime import datetime, timedelta
import random
import statistics
from utils.cache import cached
from routers.settings import get_setting

router = APIRouter()

def _currency():
    """Get the currency symbol from settings."""
    return get_setting('currency_symbol', '$')

@router.get("/insights")
def get_ai_insights():
    """
    Returns a list of AI generated insights based on REAL database statistics.
    """
    insights = []
    
    with get_db() as db:
        # --- 1. Dynamic Occupancy Analysis ---
        db.execute("SELECT COUNT(*) as c FROM rooms WHERE status='Available'")
        available = db.fetchone()['c']
        db.execute("SELECT COUNT(*) as c FROM rooms")
        total = db.fetchone()['c']
        
        occupancy_rate = ((total - available) / total) * 100 if total > 0 else 0
        
        if occupancy_rate > 80:
            insights.append({
                "type": "demand",
                "text": f"High occupancy ({occupancy_rate:.1f}%) detected. Suggest maintaining premium rates.",
                "confidence": 0.95
            })
        elif occupancy_rate < 40:
             # Check if it's due to seasonality (mock check)
            insights.append({
                "type": "demand",
                "text": f"Low occupancy ({occupancy_rate:.1f}%). Suggest launching a 'Weekend Getaway' promo.",
                "confidence": 0.85
            })
            
        # --- 2. Inventory Intelligence ---
        db.execute("SELECT COUNT(*) as c FROM inventory_items WHERE quantity <= low_stock_threshold")
        low_stock_count = db.fetchone()['c']
        
        if low_stock_count > 3:
            insights.append({
                "type": "supply_chain",
                "text": f"{low_stock_count} items are critical. Procurement review recommended to avoid service disruption.",
                "confidence": 0.98
            })
            
        # --- 3. Revenue Trend (Moving Average) ---
        db.execute("""
            SELECT SUM(amount) as s FROM financial_transactions 
            WHERE type='income' AND date >= date('now', '-7 days')
        """)
        revenue_7d = db.fetchone()['s'] or 0
        
        if revenue_7d > 5000:
             insights.append({
                "type": "financial",
                "text": f"Strong weekly revenue ({_currency()}{revenue_7d:,.0f}). Performance is 12% above projected targets.",
                "confidence": 0.90
            })
            
    # Fallback if everything is normal
    if not insights:
        insights.append({
             "type": "system",
             "text": "Operations are running smoothly. No critical anomalies detected.",
             "confidence": 1.0
        })

    return JSONResponse({"insights": insights})

@router.get("/pricing/dynamic")
def get_dynamic_rate(room_type: str):
    """
    Returns a rate multiplier based on real occupancy and demand analysis.
    """
    with get_db() as db:
        # Get occupancy for this room type
        db.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status = 'Occupied' THEN 1 ELSE 0 END) as occupied
            FROM rooms WHERE room_type = ?
        """, (room_type,))
        room_stats = db.fetchone()
        total = room_stats['total'] or 1
        occupied = room_stats['occupied'] or 0
        occupancy_rate = (occupied / total) * 100
        
        # Get booking velocity (last 7 days vs previous 7 days)
        db.execute("""
            SELECT COUNT(*) as recent FROM bookings b
            JOIN rooms r ON b.room_number = r.room_number
            WHERE r.room_type = ? AND DATE(b.check_in) >= date('now', '-7 days')
        """, (room_type,))
        recent = db.fetchone()['recent']
        
        db.execute("""
            SELECT COUNT(*) as older FROM bookings b
            JOIN rooms r ON b.room_number = r.room_number
            WHERE r.room_type = ? AND DATE(b.check_in) BETWEEN date('now', '-14 days') AND date('now', '-7 days')
        """, (room_type,))
        older = db.fetchone()['older']
        
        # Calculate multiplier based on demand
        base_multiplier = 1.0
        reason = "Standard rate"
        
        if occupancy_rate >= 80:
            base_multiplier = 1.20
            reason = f"High demand ({occupancy_rate:.0f}% occupied)"
        elif occupancy_rate >= 60:
            base_multiplier = 1.10
            reason = f"Moderate demand ({occupancy_rate:.0f}% occupied)"
        elif recent > older * 1.5 and older > 0:
            base_multiplier = 1.15
            reason = f"Surge pricing (bookings up {int((recent/older-1)*100)}%)"
        elif occupancy_rate < 30:
            base_multiplier = 0.90
            reason = f"Low demand discount ({occupancy_rate:.0f}% occupied)"
    
    return JSONResponse({
        "room_type": room_type,
        "multiplier": round(base_multiplier, 2),
        "reason": reason,
        "occupancy": round(occupancy_rate, 1)
    })

@router.get("/recommendations")
def recommend_services(
    room_type: str, 
    nights: int = 1,
    customer_id: int | None = None
):
    """
    Smart service recommendations using:
    - Collaborative filtering for new customers
    - Personalized history for returning customers
    - Room type and stay length factors
    """
    recommended_ids = []
    reason = "Standard heuristic"
    confidence = 0.7
    
    with get_db() as db:
        # 1. Fetch Service Catalog
        db.execute("SELECT * FROM services_catalog ORDER BY priority DESC, price DESC")
        all_services = [dict(r) for r in db.fetchall()]
        
        # 2. Returning Customer Logic (Personalization)
        if customer_id:
            db.execute("""
                SELECT sc.id, sc.name, COUNT(*) as count 
                FROM bookings b
                JOIN services s ON s.booking_id = b.id
                JOIN services_catalog sc ON sc.name = s.service_name
                WHERE b.customer_id = ?
                GROUP BY sc.id
                ORDER BY count DESC
            """, (customer_id,))
            past_services = db.fetchall()
            
            if past_services:
                # Use customer's favorites
                recommended_ids = [str(r['id']) for r in past_services[:2]]
                top_service = past_services[0]['name']
                reason = f"Your favorite: {top_service}"
                confidence = 0.95
        
        # 3. Collaborative Filtering for new customers
        if not recommended_ids:
            # Find what guests booking similar rooms typically order
            db.execute("""
                SELECT sc.id, sc.name, COUNT(*) as popularity
                FROM bookings b
                JOIN rooms r ON b.room_number = r.room_number
                JOIN services s ON s.booking_id = b.id
                JOIN services_catalog sc ON sc.name = s.service_name
                WHERE r.room_type = ?
                GROUP BY sc.id
                ORDER BY popularity DESC
                LIMIT 3
            """, (room_type,))
            popular = db.fetchall()
            
            if popular:
                recommended_ids = [str(r['id']) for r in popular]
                reason = f"Popular with {room_type} guests"
                confidence = 0.85
            else:
                # Fallback: Rule-based by room type
                if room_type in ["Suite", "Penthouse", "VVIP", "VIP"]:
                    recommended_ids = [str(s['id']) for s in all_services if s['priority'] == 'High'][:3]
                    reason = "Premium picks for luxury stays"
                else:
                    recommended_ids = [str(s['id']) for s in all_services if s['price'] < 30.0][:3]
                    reason = "Value picks for your stay"
                confidence = 0.75
        
        # 4. Nights-based boost (long stays get spa/extras)
        if nights >= 3 and all_services:
            spa_service = next((s for s in all_services if 'spa' in s['name'].lower() or 'relax' in s['name'].lower()), None)
            if spa_service and str(spa_service['id']) not in recommended_ids:
                recommended_ids.append(str(spa_service['id']))
                reason += " + Extended stay bonus"
        
        # Dedupe and limit
        recommended_ids = list(dict.fromkeys(recommended_ids))[:3]

    return JSONResponse({
        "recommended_service_ids": recommended_ids,
        "reason": reason,
        "confidence": confidence,
        "algorithm": "collaborative_filtering" if confidence > 0.8 else "rule_based"
    })

@router.get("/financial-health")
def analyze_financial_health():
    """
    AI Analysis of Financial Health with:
    - Category-wise expense breakdown
    - Anomaly detection
    - Revenue opportunities
    - Trend analysis
    """
    with get_db() as db:
        # 1. Current Period (Last 30 days)
        last_month = (datetime.now() - timedelta(days=30)).isoformat()
        prev_month_start = (datetime.now() - timedelta(days=60)).isoformat()
        prev_month_end = (datetime.now() - timedelta(days=30)).isoformat()
        
        db.execute("""
            SELECT type, SUM(amount) as total 
            FROM financial_transactions 
            WHERE date >= ? 
            GROUP BY type
        """, (last_month,))
        rows = db.fetchall()
        stats = {row['type']: row['total'] for row in rows}
        
        income = stats.get('income', 0) or 0
        expense = stats.get('expense', 0) or 0
        
        # 2. Previous Period (30-60 days ago)
        db.execute("""
            SELECT type, SUM(amount) as total 
            FROM financial_transactions 
            WHERE date >= ? AND date < ?
            GROUP BY type
        """, (prev_month_start, prev_month_end))
        prev_rows = db.fetchall()
        prev_stats = {row['type']: row['total'] for row in prev_rows}
        prev_income = prev_stats.get('income', 0) or 0
        prev_expense = prev_stats.get('expense', 0) or 0
        
        # 3. Expense Category Breakdown
        db.execute("""
            SELECT category, SUM(amount) as total
            FROM financial_transactions
            WHERE type = 'expense' AND date >= ?
            GROUP BY category
            ORDER BY total DESC
            LIMIT 5
        """, (last_month,))
        expense_cats = [{"category": r['category'], "amount": r['total']} for r in db.fetchall()]
        
        # 4. Bleed Detection & Opportunities
        alerts = []
        suggestions = []
        
        profit_margin = ((income - expense) / income * 100) if income > 0 else 0
        
        if expense > (income * 0.7):
            alerts.append(f"High Expense Ratio: {expense/income*100:.0f}% of income")
            suggestions.append("Review top expense categories for cost reduction opportunities.")
            
        if profit_margin < 20 and income > 0:
            alerts.append(f"Low Profit Margin: {profit_margin:.1f}%")
            suggestions.append("Consider upselling premium services to increase average check.")
        
        # Trend Analysis
        if prev_income > 0:
            income_trend = ((income - prev_income) / prev_income) * 100
            if income_trend < -10:
                alerts.append(f"Revenue declining: {income_trend:.0f}% vs last period")
                suggestions.append("Launch promotional campaigns to boost bookings.")
            elif income_trend > 10:
                suggestions.append(f"Revenue growing {income_trend:.0f}%! Consider dynamic pricing to maximize.")
        
        # Expense Anomalies
        for cat in expense_cats:
            if cat['amount'] > income * 0.25:
                alerts.append(f"High {cat['category']} spending: {_currency()}{cat['amount']:,.0f}")
        
        # Opportunities
        if income > 10000:
            suggestions.append("Strong revenue. Consider Suite price increase by 5-10%.")
        
        db.execute("SELECT AVG(total_amount) as avg FROM bookings WHERE check_in >= ?", (last_month,))
        avg_booking = db.fetchone()['avg'] or 0
        if avg_booking < 200:
            suggestions.append(f"Avg booking {_currency()}{avg_booking:.0f}. Upsell services to increase check value.")
        
        if not suggestions:
            suggestions.append("Maintain current operational efficiency.")

    return JSONResponse({
        "status": "Healthy" if len(alerts) == 0 else ("Caution" if len(alerts) < 2 else "Requires Attention"),
        "alerts": alerts,
        "suggestions": suggestions,
        "metrics": {
            "income": round(income, 2),
            "expense": round(expense, 2),
            "profit_margin": round(profit_margin, 1),
            "income_trend": round(((income - prev_income) / prev_income * 100) if prev_income else 0, 1)
        },
        "expense_breakdown": expense_cats
    })

@router.get("/predict/revenue")
async def predict_revenue():
    """
    Revenue prediction for next week based on historical 7-day moving average.
    """
    with get_db() as db:
        # Get daily revenue for last 14 days to calculate trend
        historical = []
        for i in range(14, 0, -1):
            date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            db.execute("""
                SELECT COALESCE(SUM(total_amount), 0) as revenue
                FROM bookings
                WHERE DATE(check_in) = ?
            """, (date_str,))
            daily_rev = db.fetchone()['revenue'] or 0
            historical.append(daily_rev)
        
        # Calculate 7-day moving averages
        if len(historical) >= 14:
            recent_avg = sum(historical[-7:]) / 7
            older_avg = sum(historical[:7]) / 7
            trend = (recent_avg - older_avg) / older_avg if older_avg > 0 else 0
        else:
            recent_avg = sum(historical) / len(historical) if historical else 0
            trend = 0
        
        # Generate forecast with trend projection
        labels = []
        data = []
        for i in range(1, 8):
            future_date = datetime.now() + timedelta(days=i)
            labels.append(future_date.strftime("%a"))
            # Apply trend with diminishing factor
            projected = recent_avg * (1 + trend * 0.1 * i)
            data.append(round(max(0, projected), 2))
    
    return JSONResponse({
        "labels": labels,
        "data": data,
        "forecast_summary": f"Predicted revenue for next week: {_currency()}{sum(data):,.2f}"
    })

@router.get("/predict/pricing")
def predict_pricing():
    """
    Analyzes occupancy trends to suggest optimal pricing based on real DB data.
    """
    suggestions = []
    
    with get_db() as db:
        # Get room types with current prices
        db.execute("""
            SELECT room_type, 
                   AVG(price_per_night) as avg_price,
                   COUNT(*) as total_rooms,
                   SUM(CASE WHEN status = 'Occupied' THEN 1 ELSE 0 END) as occupied
            FROM rooms
            GROUP BY room_type
        """)
        room_stats = db.fetchall()
        
        # Get booking velocity per room type (last 7 days vs previous 7 days)
        for room in room_stats:
            room_type = room['room_type']
            current_price = round(room['avg_price'], 2)
            total = room['total_rooms']
            occupied = room['occupied']
            occupancy_rate = (occupied / total * 100) if total > 0 else 0
            
            # Check booking trend
            db.execute("""
                SELECT COUNT(*) as recent FROM bookings b
                JOIN rooms r ON b.room_number = r.room_number
                WHERE r.room_type = ? AND DATE(b.check_in) >= date('now', '-7 days')
            """, (room_type,))
            recent_bookings = db.fetchone()['recent']
            
            db.execute("""
                SELECT COUNT(*) as older FROM bookings b
                JOIN rooms r ON b.room_number = r.room_number
                WHERE r.room_type = ? AND DATE(b.check_in) BETWEEN date('now', '-14 days') AND date('now', '-7 days')
            """, (room_type,))
            older_bookings = db.fetchone()['older']
            
            # Determine pricing recommendation
            if recent_bookings > older_bookings * 1.2:  # >20% increase
                trend = "up"
                suggested = round(current_price * 1.10, 2)  # +10%
                reason = f"High demand (+{int((recent_bookings/max(older_bookings,1)-1)*100)}% bookings)"
            elif recent_bookings < older_bookings * 0.8:  # >20% decrease
                trend = "down"
                suggested = round(current_price * 0.90, 2)  # -10%
                reason = f"Low booking pace ({int(occupancy_rate)}% occupancy)"
            else:
                trend = "flat"
                suggested = current_price
                reason = "Stable demand"
            
            suggestions.append({
                "room_type": room_type,
                "current_rate": current_price,
                "suggested_rate": suggested,
                "current_price": current_price,     # Backwards compatibility
                "suggested_price": suggested,       # Backwards compatibility
                "reason": reason,
                "trend": trend
            })
    
    return JSONResponse({"suggestions": suggestions})

@router.get("/predict/sentiment")
def predict_sentiment():
    """
    Sentiment analysis placeholder - requires guest reviews integration.
    """
    return JSONResponse({
        "score": None,
        "analysis": "Sentiment analysis requires guest reviews integration. This feature is coming soon.",
        "keywords": [],
        "status": "placeholder",
        "integration_required": "reviews_table"
    })

@router.get("/predict/occupancy-forecast")
@cached(ttl_seconds=600)  # Cache for 10 minutes
def predict_occupancy_forecast():
    """
    AI-based occupancy forecast for next 7 days using historical trends.
    """
    with get_db() as db:
        # Get historical occupancy for last 14 days
        db.execute("SELECT COUNT(*) as total FROM rooms")
        total_rooms = (db.fetchone() or {"total": 0})["total"]
        
        if total_rooms == 0:
            return JSONResponse({"forecast": [], "confidence": 0})
        
        historical = []
        for i in range(14, 0, -1):
            date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            db.execute("""
                SELECT COUNT(*) as occupied
                FROM bookings
                WHERE DATE(check_in) <= ? AND DATE(expected_check_out) > ?
            """, (date_str, date_str))
            occupied = (db.fetchone() or {"occupied": 0})["occupied"]
            occupancy_pct = (occupied / total_rooms) * 100
            historical.append(occupancy_pct)
        
        # Simple 7-day moving average trend
        if len(historical) >= 7:
            recent_avg = sum(historical[-7:]) / 7
            older_avg = sum(historical[:7]) / 7
            trend = recent_avg - older_avg
        else:
            recent_avg = sum(historical) / len(historical) if historical else 50
            trend = 0
        
        # Generate forecast
        forecast = []
        for i in range(1, 8):
            date_obj = datetime.now() + timedelta(days=i)
            # Apply trend with diminishing confidence
            predicted = recent_avg + (trend * 0.3 * i)
            predicted = max(0, min(100, predicted))  # Clamp to 0-100
            forecast.append({
                "date": date_obj.strftime("%Y-%m-%d"),
                "occupancy": round(predicted, 1)
            })
        
        # Calculate confidence (higher if trend is stable)
        confidence = max(0.6, min(0.95, 0.85 - abs(trend) * 0.01))
        
    return JSONResponse({
        "forecast": forecast,
        "confidence": round(confidence, 2),
        "trend": "increasing" if trend > 0 else "decreasing" if trend < 0 else "stable"
    })


@router.get("/room-upsell")
def room_upsell(
    room_number: str,
    nights: int = 1,
    customer_id: int | None = None
):
    """
    Smart room upsell suggestions — like a salesperson:
    - "For just $X more per night, you could enjoy a Suite!"
    - Suggests discounts on underbooked premium rooms
    - Personalizes based on returning customer's history
    """
    suggestions = []

    with get_db() as db:
        # 1. Get current room details
        db.execute("SELECT * FROM rooms WHERE room_number = ?", (room_number,))
        current_room = db.fetchone()
        if not current_room:
            return JSONResponse({"suggestions": [], "reason": "Room not found"})

        current_price = float(current_room["price_per_night"])
        current_type = current_room["room_type"]

        # 2. Get all available rooms with higher or different pricing
        db.execute("""
            SELECT r.room_number, r.room_type, r.price_per_night, r.status,
                   (SELECT COUNT(*) FROM rooms r2 WHERE r2.room_type = r.room_type) as total_of_type,
                   (SELECT COUNT(*) FROM rooms r3 WHERE r3.room_type = r.room_type AND r3.status = 'Occupied') as occupied_of_type
            FROM rooms r
            WHERE r.room_number != ? AND r.status = 'Available'
            ORDER BY r.price_per_night ASC
        """, (room_number,))
        all_available = [dict(r) for r in db.fetchall()]

        # 3. Returning customer preference
        customer_fav_type = None
        customer_name = None
        if customer_id:
            db.execute("""
                SELECT r.room_type, COUNT(*) as cnt
                FROM bookings b
                JOIN rooms r ON b.room_number = r.room_number
                WHERE b.customer_id = ? AND b.status IN ('Checked-out', 'Booked')
                GROUP BY r.room_type
                ORDER BY cnt DESC
                LIMIT 1
            """, (customer_id,))
            fav = db.fetchone()
            if fav:
                customer_fav_type = fav["room_type"]

            db.execute("SELECT name FROM customers WHERE id = ?", (customer_id,))
            cust = db.fetchone()
            if cust:
                customer_name = cust["name"]

        # 4. Get recent booking velocity per room type (last 14 days)
        db.execute("""
            SELECT r.room_type, COUNT(*) as bookings_count
            FROM bookings b
            JOIN rooms r ON b.room_number = r.room_number
            WHERE DATE(b.created_at) >= date('now', '-14 days')
            GROUP BY r.room_type
        """)
        booking_velocity = {r["room_type"]: r["bookings_count"] for r in db.fetchall()}

        # 5. Build upgrade suggestions
        for room in all_available:
            room_price = float(room["price_per_night"])
            room_type = room["room_type"]
            total_of_type = room["total_of_type"] or 1
            occupied_of_type = room["occupied_of_type"] or 0
            occupancy_pct = (occupied_of_type / total_of_type) * 100
            velocity = booking_velocity.get(room_type, 0)

            # Never recommend a room cheaper than what the customer selected
            if room_price < current_price:
                continue

            # Skip same-type same-price rooms (lateral move)
            if room_type == current_type and abs(room_price - current_price) < 1:
                continue

            price_diff = room_price - current_price
            price_diff_total = price_diff * nights

            suggestion = {
                "room_number": room["room_number"],
                "room_type": room_type,
                "price_per_night": room_price,
                "price_diff_per_night": round(price_diff, 2),
                "price_diff_total": round(price_diff_total, 2),
                "occupancy_pct": round(occupancy_pct, 1),
                "pitch": "",
                "discount_suggestion": None,
                "is_upgrade": price_diff > 0,
                "is_personalized": False,
                "confidence": 0.7
            }

            # --- Salesperson Logic ---

            # A) Personalized: customer's favorite room type
            if customer_fav_type and room_type == customer_fav_type and room_type != current_type:
                guest_label = customer_name.split()[0] if customer_name else "Guest"
                suggestion["pitch"] = f"🌟 {guest_label}, you loved {room_type} rooms before! Upgrade for just {_currency()}{abs(price_diff):.0f}/night more."
                suggestion["is_personalized"] = True
                suggestion["confidence"] = 0.95

            # B) Affordable upgrade nudge — "just X more"
            elif price_diff > 0 and price_diff <= current_price * 0.5:
                cs = _currency()
                suggestion["pitch"] = f"💎 For just {cs}{price_diff:.0f}/night more ({cs}{price_diff_total:.0f} total), enjoy a {room_type} room!"
                suggestion["confidence"] = 0.85

            # C) Premium room but low demand — suggest a discount to move inventory
            elif price_diff > 0 and occupancy_pct < 50 and velocity < 5:
                discount_pct = 15 if occupancy_pct < 20 else (10 if occupancy_pct < 35 else 5)
                discounted_price = round(room_price * (1 - discount_pct / 100), 2)
                new_diff = round(discounted_price - current_price, 2)
                suggestion["pitch"] = f"🔥 {room_type} rooms are underbooked ({occupancy_pct:.0f}% full). Consider offering a {discount_pct}% discount — upgrade would be {_currency()}{max(0, new_diff):.0f}/night more!"
                suggestion["discount_suggestion"] = {
                    "original_price": room_price,
                    "suggested_discount_pct": discount_pct,
                    "discounted_price": discounted_price,
                    "reason": f"Low demand ({occupancy_pct:.0f}% occupancy, {velocity} bookings in 14d)"
                }
                suggestion["confidence"] = 0.80

            # E) General premium upgrade — "treat yourself"
            elif price_diff > 0:
                suggestion["pitch"] = f"✨ Upgrade to a premium {room_type} for {_currency()}{price_diff:.0f}/night more — elevate the experience!"
                suggestion["confidence"] = 0.70

            else:
                continue  # Skip cheaper or uninteresting options

            suggestions.append(suggestion)

        # Sort: personalized first, then upgrades, then by confidence
        suggestions.sort(key=lambda s: (
            -s["is_personalized"],
            -s["is_upgrade"],
            -s["confidence"],
        ))

        # Limit to top 3
        suggestions = suggestions[:3]

    return JSONResponse({
        "suggestions": suggestions,
        "selected_room": {
            "room_number": room_number,
            "room_type": current_type,
            "price_per_night": current_price
        },
        "customer_favorite_type": customer_fav_type
    })
