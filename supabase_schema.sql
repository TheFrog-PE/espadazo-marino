-- ============================================================================
-- ESPADAZO MARINO - ESQUEMA DE BASE DE DATOS HIBRIDA EN SUPABASE (POSTGRESQL)
-- Compatible con ejecucion repetida (idempotente) en Supabase SQL Editor o pg8000
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.sequences (
    name TEXT PRIMARY KEY,
    val BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS public.dishes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price NUMERIC NOT NULL,
    img TEXT DEFAULT '',
    images JSONB NOT NULL DEFAULT '[]'::jsonb,
    badge TEXT DEFAULT '',
    "desc" TEXT DEFAULT '',
    options JSONB NOT NULL DEFAULT '[]'::jsonb,
    published BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public.tables (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL DEFAULT 4,
    status TEXT NOT NULL DEFAULT 'libre',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public.orders (
    id TEXT PRIMARY KEY,
    seq BIGINT,
    table_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    service_mode TEXT NOT NULL,
    time TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL,
    items JSONB NOT NULL DEFAULT '[]'::jsonb,
    total NUMERIC NOT NULL,
    notes TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pedido',
    paid BOOLEAN NOT NULL DEFAULT FALSE,
    paid_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS public.receipts (
    id TEXT PRIMARY KEY,
    seq BIGINT UNIQUE NOT NULL,
    number TEXT UNIQUE NOT NULL,
    issued_at TEXT NOT NULL,
    table_id TEXT DEFAULT '',
    table_name TEXT DEFAULT '',
    mode TEXT DEFAULT '',
    customer_name TEXT DEFAULT 'CLIENTES VARIOS',
    customer_doc TEXT DEFAULT '00000000',
    customer_phone TEXT DEFAULT '',
    payment_method TEXT DEFAULT 'YAPE / PLIN',
    op_number TEXT DEFAULT '',
    hash TEXT DEFAULT '',
    items JSONB NOT NULL DEFAULT '[]'::jsonb,
    op_gravada NUMERIC NOT NULL DEFAULT 0,
    igv NUMERIC NOT NULL DEFAULT 0,
    total NUMERIC NOT NULL DEFAULT 0
);

-- Indices para rendimiento de consultas KDS y mesas
CREATE INDEX IF NOT EXISTS idx_orders_table_paid ON public.orders(table_id, paid);
CREATE INDEX IF NOT EXISTS idx_orders_status_paid ON public.orders(status, paid);
CREATE INDEX IF NOT EXISTS idx_orders_timestamp ON public.orders(timestamp);

-- Seguridad RLS (Permisiva para demo / panel conectado con anon y authenticated)
ALTER TABLE public.settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sequences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.dishes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tables ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.receipts ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['settings', 'sequences', 'dishes', 'tables', 'orders', 'receipts']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = tbl AND policyname = 'espadazo_full_access'
        ) THEN
            EXECUTE format(
                'CREATE POLICY espadazo_full_access ON public.%I FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);',
                tbl
            );
        END IF;
    END LOOP;
END $$;

-- Bucket de almacenamiento para fotografias de platillos (dishes-media)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'storage' AND table_name = 'buckets') THEN
        INSERT INTO storage.buckets (id, name, public)
        VALUES ('dishes-media', 'dishes-media', TRUE)
        ON CONFLICT (id) DO UPDATE SET public = TRUE;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'storage' AND table_name = 'objects') THEN
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'storage' AND tablename = 'objects' AND policyname = 'dishes_media_public_read'
        ) THEN
            CREATE POLICY dishes_media_public_read ON storage.objects
                FOR SELECT TO anon, authenticated USING (bucket_id = 'dishes-media');
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'storage' AND tablename = 'objects' AND policyname = 'dishes_media_public_write'
        ) THEN
            CREATE POLICY dishes_media_public_write ON storage.objects
                FOR INSERT TO anon, authenticated WITH CHECK (bucket_id = 'dishes-media');
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'storage' AND tablename = 'objects' AND policyname = 'dishes_media_public_update'
        ) THEN
            CREATE POLICY dishes_media_public_update ON storage.objects
                FOR UPDATE TO anon, authenticated USING (bucket_id = 'dishes-media') WITH CHECK (bucket_id = 'dishes-media');
        END IF;
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Omitiendo configuracion de storage: %', SQLERRM;
END $$;

-- Habilitar Supabase Realtime para sincronizacion instantanea
DO $$
DECLARE
    tbl TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        FOREACH tbl IN ARRAY ARRAY['orders', 'tables', 'dishes', 'receipts', 'settings']
        LOOP
            IF NOT EXISTS (
                SELECT 1
                FROM pg_publication_tables
                WHERE pubname = 'supabase_realtime' AND schemaname = 'public' AND tablename = tbl
            ) THEN
                EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I;', tbl);
            END IF;
        END LOOP;
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Omitiendo configuracion realtime: %', SQLERRM;
END $$;
