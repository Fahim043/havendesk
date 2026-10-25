# routers/inventory.py
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from dependencies import get_db
from datetime import datetime, timedelta

templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now
router = APIRouter()

@router.get("/")
def list_inventory(request: Request, sort_by: str = "name", sort_order: str = "asc"):
    # Validate sort parameters
    allowed_sort_cols = {"name": "name", "current_stock": "current_stock", "unit": "unit", "last_updated": "last_updated", "unit_price": "unit_price"}
    sort_col = allowed_sort_cols.get(sort_by, "name")
    order = "DESC" if sort_order.lower() == "desc" else "ASC"
    
    with get_db() as db:
        db.execute(f"SELECT * FROM inventory ORDER BY {sort_col} {order}")
        items = db.fetchall()
        
        # Calculate low stock count
        low_stock_count = sum(1 for i in items if i["current_stock"] <= (i["par_level"] or 0) * 0.3)

        # Get currency symbol
        db.execute("SELECT value FROM settings WHERE key = 'currency_symbol'")
        row = db.fetchone()
        currency = row["value"] if row else "$"
    
    return templates.TemplateResponse("inventory.html", {
        "request": request, 
        "items": items,
        "low_stock_count": low_stock_count,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "currency": currency
    })


@router.get("/insights")
def inventory_insights():
    """
    Get AI-powered inventory insights using real usage data
    """
    with get_db() as db:
        # Low Stock Items
        db.execute("""
            SELECT name, current_stock, par_level, unit,
                   ROUND((current_stock * 100.0 / NULLIF(par_level, 0)), 1) as stock_percentage
            FROM inventory
            WHERE par_level > 0 AND current_stock < (par_level * 0.3)
            ORDER BY stock_percentage ASC
        """)
        low_stock = [dict(row) for row in db.fetchall()]
        
        # Dead Stock - high stock with low usage in last 30 days
        db.execute("""
            SELECT i.name, i.current_stock, i.par_level, i.unit,
                   COALESCE(SUM(u.quantity), 0) as usage_count
            FROM inventory i
            LEFT JOIN inventory_usage u ON i.id = u.inventory_id 
                AND DATE(u.usage_date) >= date('now', '-30 days')
            WHERE i.current_stock > (i.par_level * 0.8)
            GROUP BY i.id
            ORDER BY usage_count ASC, i.current_stock DESC
            LIMIT 5
        """)
        dead_stock = [dict(row) for row in db.fetchall()]
        
        # High Demand - items with high usage in last 30 days
        db.execute("""
            SELECT i.name, i.current_stock, i.par_level, i.unit,
                   COALESCE(SUM(u.quantity), 0) as usage_count
            FROM inventory i
            LEFT JOIN inventory_usage u ON i.id = u.inventory_id
                AND DATE(u.usage_date) >= date('now', '-30 days')
            WHERE i.current_stock < i.par_level AND i.current_stock > 0
            GROUP BY i.id
            ORDER BY usage_count DESC
            LIMIT 5
        """)
        high_demand = [dict(row) for row in db.fetchall()]
    
    return JSONResponse({
        "low_stock": low_stock,
        "dead_stock": dead_stock,
        "high_demand": high_demand
    })


@router.post("/add")
def add_item(
    item_name: str = Form(...),
    current_stock: int = Form(...),
    unit: str = Form(...),
    low_stock_threshold: int = Form(10),
    supplier: str = Form(None),
    unit_price: float = Form(0),
):
    now = datetime.now().isoformat()
    total_cost = unit_price * current_stock if unit_price > 0 else 0
    
    with get_db() as db:
        db.execute("""
            INSERT INTO inventory (name, current_stock, unit, par_level, supplier, unit_price, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (item_name, current_stock, unit, low_stock_threshold, supplier, unit_price, now))
        
        # Auto-register as expense in financial_transactions if there's a cost
        if total_cost > 0:
            db.execute("""
                INSERT INTO financial_transactions (type, amount, category, description, date, entered_by)
                VALUES ('expense', ?, 'Inventory', ?, ?, 'System')
            """, (total_cost, f"Inventory purchase: {item_name} ({current_stock} {unit} × {unit_price})", now))
    
    return RedirectResponse("/inventory", status_code=303)


@router.post("/update")
def update_item(
    id: int = Form(...),
    # Optional fields for full edit
    item_name: str = Form(None),
    current_stock: str = Form(None),
    unit: str = Form(None),
    low_stock_threshold: str = Form(None),
    supplier: str = Form(None),
    unit_price: str = Form(None),
    # For quick +/- buttons
    change: int = Form(None),
    # For detailed modal adjustments
    add_quantity: str = Form(None),
    deduct_quantity: str = Form(None),
    # Stock adjustment (removal/correction with finance tracking)
    stock_adjustment: str = Form(None),
    adjustment_reason: str = Form(None),
):
    now = datetime.now().isoformat()
    
    def to_int(val):
        if val is None or val == "": return 0
        return int(val)
        
    def to_int_none(val):
        if val is None or val == "": return None
        return int(val)

    def to_float_none(val):
        if val is None or val == "": return None
        return float(val)

    with get_db() as db:
        if change is not None:
            # Quick stock update (+/-)
            db.execute("SELECT * FROM inventory WHERE id = ?", (id,))
            item = db.fetchone()
            db.execute("UPDATE inventory SET current_stock = current_stock + ?, last_updated = ? WHERE id = ?", 
                       (change, now, id))
            # Record usage if decreasing
            if change < 0:
                db.execute("INSERT INTO inventory_usage (inventory_id, quantity, usage_date) VALUES (?, ?, ?)",
                           (id, abs(change), now))
            # Register finance entry for stock INCREASE only
            if change > 0 and item:
                price = float(item['unit_price'] or 0)
                if price > 0:
                    cost = change * price
                    db.execute("""
                        INSERT INTO financial_transactions (type, amount, category, description, date, entered_by)
                        VALUES ('expense', ?, 'Inventory', ?, ?, 'System')
                    """, (cost, f"Quick restock: {item['name']} (+{change} {item['unit']} × {price})", now))
        elif to_int(stock_adjustment) > 0:
            # Stock Adjustment — remove stock and record as 'adjustment' (not expense)
            adj_qty = to_int(stock_adjustment)
            reason = (adjustment_reason or '').strip() or 'Stock adjustment'
            db.execute("SELECT * FROM inventory WHERE id = ?", (id,))
            item = db.fetchone()
            if item and adj_qty > 0:
                price = float(item['unit_price'] or 0)
                adj_value = adj_qty * price if price > 0 else 0
                # Reduce stock
                db.execute("UPDATE inventory SET current_stock = MAX(0, current_stock - ?), last_updated = ? WHERE id = ?",
                           (adj_qty, now, id))
                # Record usage
                db.execute("INSERT INTO inventory_usage (inventory_id, quantity, usage_date) VALUES (?, ?, ?)",
                           (id, adj_qty, now))
                # Record in finance as negative expense to reduce total expense (corrects over-adding)
                if adj_value > 0:
                    db.execute("""
                        INSERT INTO financial_transactions (type, amount, category, description, date, entered_by)
                        VALUES ('expense', ?, 'Inventory', ?, ?, 'System')
                    """, (-adj_value, f"⚠️ Stock Adjustment: {item['name']} (-{adj_qty} {item['unit']}) — {reason}", now))
        else:
            # Full edit
            if item_name:
                # 1. Determine new stock level
                add = to_int(add_quantity)
                deduct = to_int(deduct_quantity)
                
                # Fetch current DB record to support partial updates and fallbacks
                db.execute("SELECT * FROM inventory WHERE id = ?", (id,))
                row = db.fetchone()
                
                if not row:
                     # Item not found, redirect or error
                     return RedirectResponse("/inventory", status_code=303)

                # Handle case where record exists but current_stock is NULL
                val = row['current_stock']
                db_stock = int(val) if val is not None else 0
                
                final_stock = db_stock
                
                if add > 0 or deduct > 0:
                    # Priority 1: Relative Adjustment
                    final_stock = db_stock + add - deduct
                    
                    # Record usage if deducted
                    if deduct > 0:
                         db.execute("INSERT INTO inventory_usage (inventory_id, quantity, usage_date) VALUES (?, ?, ?)",
                           (id, deduct, now))
                    
                    # Auto-register add stock as expense if unit_price available
                    if add > 0:
                        price = to_float_none(unit_price) or (row.get('unit_price') or 0)
                        if price > 0:
                            cost = add * price
                            db.execute("""
                                INSERT INTO financial_transactions (type, amount, category, description, date, entered_by)
                                VALUES ('expense', ?, 'Inventory', ?, ?, 'System')
                            """, (cost, f"Restock: {item_name} (+{add} {row['unit']} × {price})", now))
                else:
                    # Priority 2: Absolute Correction (if provided)
                    c_stock_val = to_int_none(current_stock)
                    if c_stock_val is not None:
                        final_stock = c_stock_val

                # 2. Update all fields (Fallback to existing values if form fields are empty/None)
                final_unit = unit if unit else row['unit']
                final_supplier = supplier if supplier else row['supplier']
                
                par_val = to_int_none(low_stock_threshold)
                final_par = par_val if par_val is not None else row['par_level']
                
                price_val = to_float_none(unit_price)
                final_price = price_val if price_val is not None else (row.get('unit_price') or 0)
                
                db.execute("""
                    UPDATE inventory 
                    SET name = ?, current_stock = ?, unit = ?, par_level = ?, supplier = ?, unit_price = ?, last_updated = ?
                    WHERE id = ?
                """, (item_name, final_stock, final_unit, final_par, final_supplier, final_price, now, id))
                
    return RedirectResponse("/inventory", status_code=303)


@router.post("/delete")
def delete_item(id: int = Form(...)):
    with get_db() as db:
        db.execute("DELETE FROM inventory WHERE id = ?", (id,))
    return RedirectResponse("/inventory", status_code=303)
