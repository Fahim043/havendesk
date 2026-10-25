CREATE TABLE inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 0,
        unit TEXT NOT NULL,
        low_stock_threshold INTEGER DEFAULT 10,
        supplier TEXT,
        last_updated TEXT
    );
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_number TEXT UNIQUE NOT NULL,
        room_type TEXT NOT NULL,
        price_per_night REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'Available',
        housekeeping_status TEXT NOT NULL DEFAULT 'Clean',
        created_at TEXT,
        updated_at TEXT
    );
CREATE TABLE customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        phone TEXT,
        id_proof TEXT,
        address TEXT,
        created_at TEXT,
        updated_at TEXT
    , customer_type TEXT, loyalty_points INTEGER DEFAULT 0, marketing_opt_out INTEGER DEFAULT 0);
CREATE TABLE booking_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        label TEXT,
        created_at TEXT,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    );
CREATE TABLE bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        room_number TEXT NOT NULL,
        check_in TEXT,
        expected_check_out TEXT,
        actual_check_out TEXT,
        room_rate REAL NOT NULL,
        num_nights INTEGER NOT NULL DEFAULT 1,
        extra_charges TEXT,        -- JSON string
        total_amount REAL,
        advance_amount REAL DEFAULT 0,
        amount_paid REAL DEFAULT 0,
        payment_status TEXT DEFAULT 'Unpaid',  -- 'Unpaid', 'Partial', 'Paid'
        group_id INTEGER,
        status TEXT,
        created_at TEXT,
        updated_at TEXT, payment_method TEXT, pax INTEGER DEFAULT 1, reference_id INTEGER,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
        FOREIGN KEY (room_number) REFERENCES rooms(room_number) ON UPDATE CASCADE,
        FOREIGN KEY (group_id) REFERENCES booking_groups(id) ON DELETE SET NULL
    );
CREATE TABLE services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id INTEGER,
        service_name TEXT,
        price REAL,
        date TEXT,
        FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE
    );
CREATE TABLE payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_method TEXT,
        payment_type TEXT,  -- 'advance', 'partial', 'final'
        notes TEXT,
        created_at TEXT,
        FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE
    );
CREATE TABLE maintenance_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER,
        task_description TEXT,
        status TEXT,
        assigned_to TEXT,
        created_at TEXT,
        FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE SET NULL
    );
CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash BLOB,
        role TEXT,
        created_at TEXT
    );
CREATE TABLE audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        timestamp TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
    );
CREATE TABLE daily_revenue_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT UNIQUE,
        total_revenue REAL
    );
CREATE TABLE inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT,
        current_stock INTEGER NOT NULL DEFAULT 0,
        par_level INTEGER DEFAULT 0,
        unit TEXT NOT NULL,
        unit_price REAL DEFAULT 0,
        supplier TEXT,
        last_updated TEXT
    );
CREATE TABLE inventory_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_id INTEGER,
        quantity INTEGER,
        usage_date TEXT,
        used_by TEXT,
        FOREIGN KEY (inventory_id) REFERENCES inventory(id) ON DELETE CASCADE
    );
CREATE TABLE financial_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL, -- 'income' or 'expense'
        amount REAL NOT NULL,
        category TEXT,
        description TEXT,
        date TEXT,
        entered_by TEXT,
        related_booking_id INTEGER,
        FOREIGN KEY (related_booking_id) REFERENCES bookings(id) ON DELETE SET NULL
    );
CREATE TABLE ai_insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        insight_type TEXT, -- 'occupancy', 'pricing', 'sentiment'
        content TEXT,
        confidence_score REAL,
        created_at TEXT
    );
CREATE TABLE services_catalog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        category TEXT,
        priority TEXT DEFAULT 'Normal', -- 'High', 'Normal'
        description TEXT,
        created_at TEXT
    );
CREATE INDEX idx_rooms_status ON rooms(status);
CREATE INDEX idx_bookings_customer ON bookings(customer_id);
CREATE INDEX idx_bookings_room ON bookings(room_number);
CREATE TABLE campaign_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
CREATE TABLE campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                channel TEXT NOT NULL,
                wa_mode TEXT,
                subject TEXT,
                body TEXT,
                filters_json TEXT,
                status TEXT,
                created_at TEXT,
                completed_at TEXT
            );
CREATE TABLE campaign_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                customer_id INTEGER,
                name TEXT,
                channel TEXT,
                address TEXT,
                subject TEXT,
                message TEXT,
                status TEXT,
                error TEXT,
                sent_at TEXT
            );
