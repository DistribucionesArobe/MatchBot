-- ============================================================
-- MatchBot - Schema v1.0
-- Sistema de reservas de padel via WhatsApp
-- https://matchbot.live
-- ============================================================

-- ============================================================
-- 1. CLUBS (multi-tenant: cada club es un tenant)
-- ============================================================
CREATE TABLE IF NOT EXISTS clubs (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    slug            VARCHAR(100) UNIQUE NOT NULL,       -- "club-de-padel-victoria"
    phone_wa        VARCHAR(20),                         -- WhatsApp Business number
    wa_phone_id     VARCHAR(50),                         -- Meta WhatsApp phone_number_id
    wa_token        TEXT,                                 -- Meta WhatsApp access token
    timezone        VARCHAR(50) DEFAULT 'America/Monterrey',
    currency        VARCHAR(3) DEFAULT 'MXN',
    logo_url        TEXT,
    address         TEXT,
    active          BOOLEAN DEFAULT TRUE,
    config          JSONB DEFAULT '{}'::jsonb,           -- misc settings
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 2. COURTS (canchas)
-- ============================================================
CREATE TYPE court_type AS ENUM ('open', 'covered');       -- abierta / techada

CREATE TABLE IF NOT EXISTS courts (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    name            VARCHAR(200) NOT NULL,                -- "Cancha Techada 4 - Aceromax"
    short_name      VARCHAR(50),                          -- "Cancha 4"
    court_type      court_type DEFAULT 'open',
    sponsor         VARCHAR(200),                         -- "Aceromax"
    sort_order      INTEGER DEFAULT 0,                    -- display order in calendar
    active          BOOLEAN DEFAULT TRUE,
    features        JSONB DEFAULT '[]'::jsonb,            -- ["lighting","covered","glass"]
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_courts_club ON courts(club_id);

-- ============================================================
-- 3. COURT SCHEDULES (horarios y precios por cancha)
-- ============================================================
CREATE TABLE IF NOT EXISTS court_schedules (
    id              SERIAL PRIMARY KEY,
    court_id        INTEGER NOT NULL REFERENCES courts(id),
    day_of_week     SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),  -- 0=lunes
    open_time       TIME NOT NULL,
    close_time      TIME NOT NULL,
    slot_minutes    INTEGER DEFAULT 90,                   -- duración de slot
    price_cents     INTEGER NOT NULL,                     -- precio en centavos MXN
    peak_price_cents INTEGER,                             -- precio hora pico (nullable)
    peak_start      TIME,
    peak_end        TIME,
    active          BOOLEAN DEFAULT TRUE,
    UNIQUE(court_id, day_of_week)
);

CREATE INDEX idx_schedules_court ON court_schedules(court_id);

-- ============================================================
-- 4. BOOKINGS (reservas)
-- ============================================================
CREATE TYPE booking_status AS ENUM (
    'pending',      -- esperando pago
    'confirmed',    -- pagado
    'cancelled',    -- cancelada
    'completed',    -- ya jugaron
    'no_show'       -- no se presentaron
);

CREATE TYPE payment_method AS ENUM (
    'cash',         -- efectivo en club
    'transfer',     -- transferencia bancaria
    'card',         -- tarjeta (Stripe)
    'free'          -- cortesía / campamento
);

CREATE TABLE IF NOT EXISTS bookings (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    court_id        INTEGER NOT NULL REFERENCES courts(id),
    customer_id     INTEGER REFERENCES customers(id),
    booking_date    DATE NOT NULL,
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    status          booking_status DEFAULT 'pending',
    payment_status  VARCHAR(20) DEFAULT 'unpaid',         -- unpaid, paid, refunded
    payment_method  payment_method,
    amount_cents    INTEGER DEFAULT 0,
    notes           TEXT,
    booking_type    VARCHAR(50) DEFAULT 'regular',        -- regular, campamento, torneo, clase
    booked_via      VARCHAR(20) DEFAULT 'whatsapp',       -- whatsapp, admin, web
    wa_phone        VARCHAR(20),                          -- teléfono del que reservó por WA
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    cancelled_at    TIMESTAMPTZ
);

CREATE INDEX idx_bookings_club_date ON bookings(club_id, booking_date);
CREATE INDEX idx_bookings_court_date ON bookings(court_id, booking_date);
CREATE INDEX idx_bookings_customer ON bookings(customer_id);
CREATE INDEX idx_bookings_status ON bookings(status);

-- Prevent overlapping bookings on same court
CREATE UNIQUE INDEX idx_no_overlap ON bookings(court_id, booking_date, start_time)
    WHERE status NOT IN ('cancelled');

-- ============================================================
-- 5. CUSTOMERS (clientes)
-- ============================================================
CREATE TABLE IF NOT EXISTS customers (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    name            VARCHAR(200),
    phone           VARCHAR(20) NOT NULL,                 -- WhatsApp phone (unique per club)
    email           VARCHAR(200),
    notes           TEXT,
    total_bookings  INTEGER DEFAULT 0,
    total_spent     INTEGER DEFAULT 0,                    -- centavos
    last_booking    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(club_id, phone)
);

CREATE INDEX idx_customers_club ON customers(club_id);
CREATE INDEX idx_customers_phone ON customers(phone);

-- ============================================================
-- 6. BOOKING STATE (estado de conversación WhatsApp)
-- ============================================================
CREATE TABLE IF NOT EXISTS wa_booking_state (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    wa_phone        VARCHAR(20) NOT NULL,                 -- teléfono del usuario
    state           VARCHAR(50) DEFAULT 'idle',           -- idle, choosing_date, choosing_time, choosing_court, confirming, paying
    data            JSONB DEFAULT '{}'::jsonb,            -- datos acumulados del flujo
    expires_at      TIMESTAMPTZ,                          -- auto-expire stale sessions
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(club_id, wa_phone)
);

-- ============================================================
-- 7. CONVERSATION LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS wa_messages (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    wa_phone        VARCHAR(20) NOT NULL,
    direction       VARCHAR(10) NOT NULL,                 -- 'inbound' / 'outbound'
    message_type    VARCHAR(20),                          -- text, interactive, image, etc
    content         JSONB,
    wa_message_id   VARCHAR(100),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_messages_club_phone ON wa_messages(club_id, wa_phone, created_at DESC);

-- ============================================================
-- 8. ADMIN USERS
-- ============================================================
CREATE TABLE IF NOT EXISTS admin_users (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    email           VARCHAR(200) NOT NULL UNIQUE,
    name            VARCHAR(200),
    password_hash   TEXT NOT NULL,
    role            VARCHAR(20) DEFAULT 'admin',          -- superadmin, admin, staff
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- SEED DATA: Club de Padel Victoria
-- ============================================================
INSERT INTO clubs (name, slug, timezone, currency)
VALUES ('Club de Padel Victoria', 'club-de-padel-victoria', 'America/Monterrey', 'MXN')
ON CONFLICT (slug) DO NOTHING;

-- Insert 6 courts with real data from Playtomic
INSERT INTO courts (club_id, name, short_name, court_type, sponsor, sort_order) VALUES
    (1, 'Cancha 1 - Kia CLEBER',                       'Cancha 1', 'open',    'Kia CLEBER',               1),
    (1, 'Cancha 2 - GMC',                              'Cancha 2', 'open',    'GMC',                      2),
    (1, 'Cancha Techada 3 - Honda Plaza',               'Cancha 3', 'covered', 'Honda Plaza',              3),
    (1, 'Cancha Techada 4 - Aceromax',                  'Cancha 4', 'covered', 'Aceromax',                 4),
    (1, 'Cancha Techada 5 - Hospital Providencial',     'Cancha 5', 'covered', 'Hospital Providencial',    5),
    (1, 'Cancha Techada 6 - MG Motor',                  'Cancha 6', 'covered', 'MG Motor',                 6)
ON CONFLICT DO NOTHING;

-- Default schedules: Mon-Sun, 7:00-22:00, 90min slots
-- Open courts: $350 MXN / Covered: $450 MXN (peak: $500)
INSERT INTO court_schedules (court_id, day_of_week, open_time, close_time, slot_minutes, price_cents, peak_price_cents, peak_start, peak_end)
SELECT
    c.id,
    d.dow,
    '07:00'::TIME,
    '22:00'::TIME,
    90,
    CASE WHEN c.court_type = 'open' THEN 35000 ELSE 45000 END,
    CASE WHEN c.court_type = 'open' THEN 40000 ELSE 50000 END,
    '18:00'::TIME,
    '22:00'::TIME
FROM courts c
CROSS JOIN generate_series(0, 6) AS d(dow)
WHERE c.club_id = 1
ON CONFLICT (court_id, day_of_week) DO NOTHING;
