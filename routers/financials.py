# routers/financials.py
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from dependencies import get_db
from datetime import datetime

templates = Jinja2Templates(directory="templates")
templates.env.globals['now'] = datetime.now
router = APIRouter()

@router.get("/")
def list_financials(
    request: Request, 
    page: int = 1, 
    limit: int = 20,
    search: str = "",
    start_date: str = "",
    end_date: str = "",
    type_filter: str = "",
    sort_by: str = "date",
    sort_order: str = "desc"
):
    offset = (page - 1) * limit
    
    # Validate sort parameters
    allowed_sort_cols = {"date": "date", "amount": "amount", "category": "category"}
    sort_col = allowed_sort_cols.get(sort_by, "date")
    order = "DESC" if sort_order.lower() == "desc" else "ASC"
    
    # Build dynamic WHERE clause
    where_clauses = []
    params = []
    
    if search:
        where_clauses.append("(description LIKE ? OR category LIKE ? OR entered_by LIKE ?)")
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term])
    
    if start_date:
        where_clauses.append("date >= ?")
        params.append(start_date)
    
    if end_date:
        where_clauses.append("date <= ?")
        params.append(end_date + "T23:59:59")  # Include full end day
    
    if type_filter and type_filter in ['income', 'expense']:
        where_clauses.append("type = ?")
        params.append(type_filter)
    
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    
    with get_db() as db:
        # Sort by date + id DESC for proper datetime-based ordering
        order_clause = f"{sort_col} {order}, id {order}"
        
        query = f"SELECT * FROM financial_transactions WHERE {where_sql} ORDER BY {order_clause} LIMIT ? OFFSET ?"
        db.execute(query, params + [limit, offset])
        transactions = db.fetchall()
        
        count_query = f"SELECT COUNT(*) as c FROM financial_transactions WHERE {where_sql}"
        db.execute(count_query, params)
        total_count = db.fetchone()['c']
        
        # Calculate totals for filtered results
        income_query = f"SELECT SUM(amount) as total FROM financial_transactions WHERE type='income' AND ({where_sql})"
        db.execute(income_query, params)
        total_income = db.fetchone()['total'] or 0.0
        
        expense_query = f"SELECT SUM(amount) as total FROM financial_transactions WHERE type='expense' AND ({where_sql})"
        db.execute(expense_query, params)
        total_expense = db.fetchone()['total'] or 0.0
        
        net_profit = total_income - total_expense
        
        # Category Breakdown for Charts
        db.execute(f"SELECT category, type, SUM(amount) as total FROM financial_transactions WHERE {where_sql} GROUP BY category, type", params)
        cat_rows = db.fetchall()
        
        income_cats = {r["category"]: r["total"] for r in cat_rows if r["type"] == "income"}
        expense_cats = {r["category"]: r["total"] for r in cat_rows if r["type"] == "expense"}
        
        # Get currency symbol
        db.execute("SELECT value FROM settings WHERE key = 'currency_symbol'")
        row = db.fetchone()
        currency = row["value"] if row else "$"
        
    total_pages = (total_count + limit - 1) // limit if total_count > 0 else 1
    
    return templates.TemplateResponse("financials.html", {
        "request": request, 
        "transactions": transactions,
        "total_income": total_income,
        "total_expense": total_expense,
        "net_profit": net_profit,
        "page": page,
        "total_pages": total_pages,
        "search": search,
        "start_date": start_date,
        "end_date": end_date,
        "type_filter": type_filter,
        "income_cats": income_cats,
        "expense_cats": expense_cats,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "currency": currency
    })


@router.get("/export")
def export_financials():
    with get_db() as db:
        db.execute("SELECT * FROM financial_transactions ORDER BY date DESC, id DESC")
        rows = db.fetchall()
    
    # Simple CSV construction
    output = "ID,Date,Type,Category,Description,Amount,Entered By\n"
    for row in rows:
        r = dict(row)
        entered_by = r.get('entered_by', '') or ''
        output += f"{r['id']},{r['date']},{r['type']},{r['category']},{str(r['description']).replace(',', ';')},{r['amount']},{entered_by}\n"
    

    return Response(
        content=output, 
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=financials.csv"}
    )

@router.post("/add")
def add_transaction(
    type: str = Form(...), # 'income' or 'expense'
    amount: float = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    date: str = Form(None),
    entered_by: str = Form(""),
):
    if not date:
        date = datetime.now().isoformat()
    else:
        # If only date provided (no time), append current time
        if len(date) <= 10:
            date = date + "T" + datetime.now().strftime("%H:%M:%S")
        
    with get_db() as db:
        db.execute("""
            INSERT INTO financial_transactions (type, amount, category, description, date, entered_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (type, amount, category, description, date, entered_by or None))
    return RedirectResponse("/financials", status_code=303)
