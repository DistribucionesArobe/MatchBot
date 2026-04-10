-- ============================================================
-- MatchBot - Migration 002: Monederos + Patrocinios
-- ============================================================

-- ── MONEDEROS (Wallets) ──
ALTER TABLE customers ADD COLUMN IF NOT EXISTS wallet_balance INTEGER DEFAULT 0;  -- centavos MXN

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id              SERIAL PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    type            VARCHAR(20) NOT NULL,        -- 'topup', 'payment', 'refund'
    amount_cents    INTEGER NOT NULL,             -- positivo=ingreso, negativo=cargo
    balance_after   INTEGER NOT NULL,             -- saldo después de transacción
    description     TEXT,
    reference_id    INTEGER,                      -- booking_id si aplica
    created_by      VARCHAR(50),                  -- 'admin', 'whatsapp', 'system'
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_wallet_tx_customer ON wallet_transactions(customer_id, created_at DESC);

-- ── TIPOS DE CLIENTE (para promociones) ──
CREATE TABLE IF NOT EXISTS customer_types (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    name            VARCHAR(100) NOT NULL,        -- 'Frecuente', 'Miembro', 'Estudiante'
    discount_pct    INTEGER DEFAULT 0,            -- porcentaje de descuento
    conditions      TEXT,                          -- descripción de condición
    time_start      TIME,                         -- hora inicio aplica (null = todo el día)
    time_end        TIME,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE customers ADD COLUMN IF NOT EXISTS customer_type_id INTEGER REFERENCES customer_types(id);

-- ── PATROCINIOS ──
CREATE TABLE IF NOT EXISTS sponsorships (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    court_id        INTEGER REFERENCES courts(id),
    company_name    VARCHAR(200) NOT NULL,
    contact_name    VARCHAR(200),
    contact_phone   VARCHAR(50),
    contact_email   VARCHAR(200),
    amount_cents    INTEGER NOT NULL DEFAULT 0,   -- monto mensual en centavos
    start_date      DATE,
    end_date        DATE,
    active          BOOLEAN DEFAULT TRUE,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sponsorship_payments (
    id              SERIAL PRIMARY KEY,
    sponsorship_id  INTEGER NOT NULL REFERENCES sponsorships(id),
    period_month    INTEGER NOT NULL,              -- 1-12
    period_year     INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL,
    paid            BOOLEAN DEFAULT FALSE,
    paid_date       DATE,
    payment_method  VARCHAR(50),
    receipt_url     TEXT,                           -- foto/pdf del comprobante
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(sponsorship_id, period_month, period_year)
);

CREATE INDEX idx_sp_payments ON sponsorship_payments(sponsorship_id, period_year, period_month);

-- ── FESTIVOS / CIERRES ──
CREATE TABLE IF NOT EXISTS holidays (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    holiday_date    DATE NOT NULL,
    reason          VARCHAR(200),
    affected_courts JSONB DEFAULT '[]'::jsonb,     -- [] = todas, [1,3] = solo esas
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(club_id, holiday_date)
);

-- ── SEED: Tipos de cliente para Club de Padel Victoria ──
INSERT INTO customer_types (club_id, name, discount_pct, conditions, active) VALUES
    (1, 'Frecuente',  10, 'Automatico al cumplir 10 reservas/mes', TRUE),
    (1, 'Miembro',    15, 'Asignacion manual por admin', TRUE),
    (1, 'Estudiante', 20, 'Horario matutino 7-14h', TRUE)
ON CONFLICT DO NOTHING;

-- ── SEED: Patrocinios actuales ──
INSERT INTO sponsorships (club_id, court_id, company_name, contact_name, contact_phone, amount_cents, start_date, end_date) VALUES
    (1, 1, 'Kia CLEBER',               'Lic. Garza',    '834 111 2222', 500000, '2026-01-01', '2026-12-31'),
    (1, 2, 'GMC',                       'Ing. Salinas',  '834 222 3333', 500000, '2026-01-01', '2026-12-31'),
    (1, 3, 'Honda Plaza',               'Sr. Ramirez',   '834 333 4444', 700000, '2026-01-01', '2026-06-30'),
    (1, 4, 'Aceromax',                  'Lic. Trevino',  '834 444 5555', 700000, '2026-01-01', '2026-12-31'),
    (1, 5, 'Hospital Providencial',     'Dr. Lopez',     '834 555 6666', 800000, '2026-01-01', '2027-03-31'),
    (1, 6, 'MG Motor',                  'Ing. Cantu',    '834 666 7777', 600000, '2026-01-01', '2026-09-30')
ON CONFLICT DO NOTHING;

-- ── SEED: Festivos 2026 ──
INSERT INTO holidays (club_id, holiday_date, reason) VALUES
    (1, '2026-05-01', 'Dia del Trabajo'),
    (1, '2026-09-16', 'Dia de Independencia'),
    (1, '2026-11-16', 'Revolucion Mexicana'),
    (1, '2026-12-25', 'Navidad')
ON CONFLICT DO NOTHING;
