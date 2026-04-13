-- ============================================================
-- MatchBot - Schema v1.2
-- Salón Multiusos Victoria como SEGUNDO CLUB (negocio aparte)
-- ============================================================

-- ============================================================
-- 1. Crear segundo club
-- ============================================================
INSERT INTO clubs (id, name, slug, timezone, phone, address)
VALUES (
    2,
    'Salón Multiusos Victoria',
    'salon-victoria',
    'America/Mexico_City',
    NULL,
    NULL
)
ON CONFLICT (id) DO NOTHING;

-- Ajustar secuencia
SELECT setval('clubs_id_seq', GREATEST((SELECT MAX(id) FROM clubs), 2));

-- ============================================================
-- 2. Tipo de recurso (el salón no es "cancha")
-- ============================================================
DO $$ BEGIN
    CREATE TYPE resource_type AS ENUM ('court', 'salon');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE courts
    ADD COLUMN IF NOT EXISTS resource_type resource_type DEFAULT 'court',
    ADD COLUMN IF NOT EXISTS capacity INTEGER,
    ADD COLUMN IF NOT EXISTS description TEXT;

-- ============================================================
-- 3. Insertar el Salón como recurso del club 2
-- ============================================================
INSERT INTO courts (club_id, name, short_name, court_type, resource_type, sort_order, capacity, description)
VALUES (
    2,
    'Salón Principal',
    'Salón',
    'covered',
    'salon',
    1,
    80,
    'Salón multiusos para eventos, clases y reuniones.'
)
ON CONFLICT DO NOTHING;

-- Horario: todos los días 8:00-23:00, slots 60 min, $200/hora
INSERT INTO court_schedules (court_id, day_of_week, open_time, close_time, slot_minutes, price_cents)
SELECT c.id, d.dow, '08:00'::TIME, '23:00'::TIME, 60, 20000
FROM courts c
CROSS JOIN generate_series(0, 6) AS d(dow)
WHERE c.club_id = 2 AND c.resource_type = 'salon'
ON CONFLICT (court_id, day_of_week) DO NOTHING;

-- ============================================================
-- 4. PACKAGES (paquetes de horas)
-- ============================================================
CREATE TABLE IF NOT EXISTS packages (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    name            VARCHAR(200) NOT NULL,
    description     TEXT,
    hours_included  INTEGER NOT NULL,
    price_cents     INTEGER NOT NULL,
    validity_days   INTEGER DEFAULT 90,
    active          BOOLEAN DEFAULT TRUE,
    sort_order      INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_packages_club ON packages(club_id);

-- ============================================================
-- 5. CUSTOMER PACKAGES (paquetes comprados)
-- ============================================================
CREATE TABLE IF NOT EXISTS customer_packages (
    id              SERIAL PRIMARY KEY,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    package_id      INTEGER NOT NULL REFERENCES packages(id),
    hours_remaining NUMERIC(5,2) NOT NULL,
    purchase_price_cents INTEGER NOT NULL,
    payment_method  payment_method,
    purchased_at    TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    active          BOOLEAN DEFAULT TRUE,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_cust_pkg_customer ON customer_packages(customer_id);
CREATE INDEX IF NOT EXISTS idx_cust_pkg_active ON customer_packages(active, expires_at);

-- ============================================================
-- 6. Link bookings ↔ paquetes
-- ============================================================
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS customer_package_id INTEGER REFERENCES customer_packages(id),
    ADD COLUMN IF NOT EXISTS hours_consumed NUMERIC(5,2);

-- ============================================================
-- 7. SEED: Paquetes default para el Salón
-- ============================================================
INSERT INTO packages (club_id, name, description, hours_included, price_cents, validity_days, sort_order)
VALUES
    (2, 'Paquete 5 horas',  '5 horas de salón. Vigencia 60 días.',    5,  90000,  60, 1),
    (2, 'Paquete 10 horas', '10 horas de salón. Vigencia 90 días.',  10, 170000,  90, 2),
    (2, 'Paquete 20 horas', '20 horas de salón. Vigencia 120 días.', 20, 320000, 120, 3)
ON CONFLICT DO NOTHING;
