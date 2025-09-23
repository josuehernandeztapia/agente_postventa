-- Minimal schema for cases in Postgres/Neon
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS cases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source text CHECK (source IN ('cliente','taller','whatsapp','make','api')) NOT NULL,
  client_id text,
  contract_id text,
  delivered_at date,
  vin text,
  plate text,
  odo_km numeric,
  falla_tipo text,
  falla_subtipo text,
  warranty_status text CHECK (warranty_status IN ('eligible','review','no_eligible')),
  status text CHECK (status IN ('open','need_info','in_progress','solved','rejected')) DEFAULT 'open',
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS case_attachments (
  id bigserial PRIMARY KEY,
  case_id uuid REFERENCES cases(id) ON DELETE CASCADE,
  kind text CHECK (kind IN ('circulacion','vin_plate','odometro','evidencia','reporte_taller')) NOT NULL,
  url text NOT NULL,
  url_hash text,
  ocr jsonb,
  meta jsonb,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS case_actions (
  id bigserial PRIMARY KEY,
  case_id uuid REFERENCES cases(id) ON DELETE CASCADE,
  action text,
  payload jsonb,
  created_by text,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS case_tags (
  case_id uuid REFERENCES cases(id) ON DELETE CASCADE,
  tag text,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_case_tag ON case_tags(tag);
CREATE INDEX IF NOT EXISTS idx_cases_type ON cases(falla_tipo);

-- Backward-safe adds
ALTER TABLE cases ADD COLUMN IF NOT EXISTS delivered_at date;
ALTER TABLE case_attachments ADD COLUMN IF NOT EXISTS url_hash text;
CREATE UNIQUE INDEX IF NOT EXISTS idx_case_attachment_case_hash ON case_attachments(case_id, url_hash);
