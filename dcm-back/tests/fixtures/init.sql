-- ComplaintAI 개발 DB 재생성 스크립트 (PostgreSQL 17 + pgvector)
-- 경고: 아래 11개 프로젝트 테이블과 그 데이터/시퀀스를 삭제하고 다시 만든다.
-- 데이터 동기화용 스키마이며, 계정/민원 데이터는 별도로 복원해야 한다.
-- 실행 전 대상 DB를 확인하고 백업한 뒤 FastAPI 및 CSV 워커를 중단한다.
-- 외부 테이블/뷰가 의존하면 CASCADE로 지우지 않고 실패하여 전체 작업을 롤백한다.
-- Docker에서는 빈 볼륨에서만 자동 실행된다. 기존 볼륨은 수동 실행이 필요하다.

BEGIN;
SET LOCAL search_path TO public, pg_catalog;
SET LOCAL lock_timeout = '10s';

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

-- 의존하는 테이블부터 삭제한다. 다른 스키마와 DB 자체는 삭제하지 않는다.
DROP TABLE IF EXISTS public.complaint_responses;
DROP TABLE IF EXISTS public.import_failures;
DROP TABLE IF EXISTS public.csv_schema_mappings;
DROP TABLE IF EXISTS public.department_documents;
DROP TABLE IF EXISTS public.organization_members;
DROP TABLE IF EXISTS public.complaints;
DROP TABLE IF EXISTS public.import_jobs;
DROP TABLE IF EXISTS public.source_files;
DROP TABLE IF EXISTS public.audit_events;
DROP TABLE IF EXISTS public.organizations;
DROP TABLE IF EXISTS public.app_users;

CREATE TABLE app_users (
  id UUID PRIMARY KEY,
  owner_id UUID NOT NULL UNIQUE,
  email TEXT NOT NULL UNIQUE,
  username TEXT UNIQUE,
  display_name TEXT,
  password_salt TEXT,
  password_hash TEXT,
  account_role TEXT NOT NULL DEFAULT 'user' CHECK (account_role IN ('user', 'admin')),
  department TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE complaints (
  id BIGSERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  content TEXT,
  summary TEXT,
  category TEXT,
  department TEXT,
  embedding vector(1024),
  source_file TEXT,
  source_row INTEGER,
  content_fingerprint CHAR(64),
  raw_content_fingerprint CHAR(64),
  dedup_compact_fingerprint CHAR(64),
  dedup_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
  processing_mode TEXT NOT NULL DEFAULT 'fallback',
  analysis_state TEXT NOT NULL DEFAULT 'completed',
  analysis_revision INTEGER NOT NULL DEFAULT 0,
  llm_model TEXT,
  embedding_model TEXT,
  prompt_version TEXT,
  analysis_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  owner_user_id UUID REFERENCES app_users(owner_id),
  complaint_status TEXT NOT NULL DEFAULT '접수' CHECK (complaint_status IN ('접수', '진행중', '완료', '취소')),
  status_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ,
  cancelled_by_role TEXT,
  cancellation_reason TEXT,
  parent_complaint_id BIGINT REFERENCES complaints(id) ON DELETE SET NULL,
  previous_context JSONB,
  original_owner_user_id UUID REFERENCES app_users(owner_id) ON DELETE SET NULL,
  restored_at TIMESTAMPTZ,
  trash_previous_status TEXT
);

CREATE TABLE source_files (
  id UUID PRIMARY KEY,
  original_name TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  mime_type TEXT,
  size_bytes BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retained_until TIMESTAMPTZ
);

CREATE TABLE import_jobs (
  id UUID PRIMARY KEY,
  source_file TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('awaiting_mapping', 'queued', 'processing', 'completed', 'failed')),
  total_rows INTEGER NOT NULL DEFAULT 0,
  completed_rows INTEGER NOT NULL DEFAULT 0,
  saved_rows INTEGER NOT NULL DEFAULT 0,
  skipped_rows INTEGER NOT NULL DEFAULT 0,
  failed_rows INTEGER NOT NULL DEFAULT 0,
  storage_path TEXT,
  encoding TEXT,
  column_mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
  schema_signature CHAR(64),
  retry_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  checkpoint_rows INTEGER NOT NULL DEFAULT 0,
  started_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  worker_id TEXT,
  owner_user_id UUID REFERENCES app_users(owner_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);

CREATE TABLE csv_schema_mappings (
  id BIGSERIAL PRIMARY KEY,
  schema_signature CHAR(64) NOT NULL UNIQUE,
  profile_name TEXT,
  column_mapping JSONB NOT NULL,
  confidence NUMERIC(3,2) NOT NULL DEFAULT 0,
  created_by_user_id UUID REFERENCES app_users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE import_failures (
  id BIGSERIAL PRIMARY KEY,
  job_id UUID NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
  source_row INTEGER NOT NULL,
  raw_data JSONB NOT NULL,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE complaint_responses (
  id UUID PRIMARY KEY,
  complaint_id BIGINT NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
  author_user_id UUID NOT NULL REFERENCES app_users(id),
  department TEXT NOT NULL,
  content TEXT NOT NULL,
  response_state TEXT NOT NULL DEFAULT 'sent' CHECK (response_state IN ('draft', 'sent')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sent_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX app_users_owner_id_idx ON app_users(owner_id);
CREATE INDEX complaints_created_at_idx ON complaints(created_at DESC);
CREATE INDEX complaints_owner_idx ON complaints(owner_user_id, deleted_at);
CREATE INDEX complaints_deleted_at_idx ON complaints(deleted_at);
CREATE INDEX complaints_trash_time_idx ON complaints(deleted_at DESC,id DESC) WHERE deleted_at IS NOT NULL;
CREATE INDEX complaints_content_fingerprint_idx ON complaints(content_fingerprint);
CREATE INDEX complaints_dedup_candidates_idx ON complaints(owner_user_id,dedup_compact_fingerprint)
  WHERE deleted_at IS NULL AND complaint_status<>'취소';
CREATE UNIQUE INDEX complaints_active_raw_unique ON complaints(owner_user_id,raw_content_fingerprint) NULLS NOT DISTINCT
  WHERE raw_content_fingerprint IS NOT NULL AND deleted_at IS NULL AND complaint_status<>'취소';
CREATE INDEX complaints_embedding_hnsw_idx ON complaints USING hnsw (embedding vector_cosine_ops);
CREATE INDEX import_jobs_owner_idx ON import_jobs(owner_user_id, created_at DESC);
CREATE INDEX import_jobs_queue_idx ON import_jobs(status, created_at) WHERE status IN ('queued', 'processing');
CREATE INDEX import_failures_job_idx ON import_failures(job_id, source_row);
CREATE INDEX csv_schema_mappings_signature_idx ON csv_schema_mappings(schema_signature);
CREATE INDEX complaint_responses_complaint_idx ON complaint_responses(complaint_id, created_at);

-- 현재 DB에 남아 있는 호환 테이블. 데이터 복원 시 구조 누락을 방지한다.
-- React/FastAPI의 제거된 부서 자료 관리 UI를 다시 활성화하지는 않는다.
CREATE TABLE organizations (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE organization_members (
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'viewer')),
  PRIMARY KEY (organization_id, user_id)
);

CREATE TABLE audit_events (
  id BIGSERIAL PRIMARY KEY,
  event_type TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT,
  detail JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE department_documents (
  id UUID PRIMARY KEY,
  document_id UUID NOT NULL,
  department TEXT NOT NULL,
  title TEXT NOT NULL,
  original_name TEXT NOT NULL,
  version INTEGER NOT NULL,
  storage_path TEXT NOT NULL,
  content TEXT NOT NULL,
  embedding vector(1024),
  created_by UUID NOT NULL REFERENCES app_users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX department_documents_version_idx ON department_documents(document_id, version);
CREATE INDEX department_documents_scope_idx ON department_documents(department, deleted_at, created_at DESC);
CREATE INDEX department_documents_embedding_hnsw_idx ON department_documents USING hnsw (embedding vector_cosine_ops);

-- Shared department-ID schema (same SQL as 20261016_department_ids.sql).
-- Stable department IDs. Additive and repeatable; never resets account/complaint data.
CREATE TABLE IF NOT EXISTS public.departments (
  id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 7),
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL UNIQUE,
  aliases TEXT[] NOT NULL DEFAULT '{}'
);
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.departments d JOIN (VALUES
    (1, 'labor_business', '노동·기업'),
    (2, 'traffic_land', '교통·국토'),
    (3, 'housing', '주택·건축'),
    (4, 'environment', '환경·위생'),
    (5, 'culture_admin_safety', '문화·행정·안전'),
    (6, 'welfare', '보건·복지'),
    (7, 'etc', '기타')
  ) AS expected(id,code,name) ON expected.id=d.id
  WHERE d.code<>expected.code OR d.name<>expected.name) THEN
    RAISE EXCEPTION 'Department IDs conflict with the fixed catalog; migration aborted';
  END IF;
END $$;
INSERT INTO public.departments(id,code,name,aliases) VALUES
  (1, 'labor_business', '노동·기업', ARRAY['노동','기업']::text[]),
  (2, 'traffic_land', '교통·국토', ARRAY['교통','건설·국토','국토·교통','국토교통']::text[]),
  (3, 'housing', '주택·건축', ARRAY['주택건축']::text[]),
  (4, 'environment', '환경·위생', ARRAY[]::text[]),
  (5, 'culture_admin_safety', '문화·행정·안전', ARRAY['행정·안전','행정안전','소방']::text[]),
  (6, 'welfare', '보건·복지', ARRAY['보건복지']::text[]),
  (7, 'etc', '기타', ARRAY[]::text[])
ON CONFLICT (id) DO NOTHING;

CREATE OR REPLACE FUNCTION public.complaintai_department_id(value TEXT)
RETURNS INTEGER LANGUAGE SQL STABLE AS $$
  SELECT id FROM public.departments
  WHERE name=BTRIM(value) OR BTRIM(value)=ANY(aliases) OR id::text=BTRIM(value)
$$;

ALTER TABLE public.app_users ADD COLUMN IF NOT EXISTS department_id INTEGER;
ALTER TABLE public.complaints ADD COLUMN IF NOT EXISTS department_id INTEGER;
ALTER TABLE public.complaint_responses ADD COLUMN IF NOT EXISTS department_id INTEGER;
ALTER TABLE public.department_documents ADD COLUMN IF NOT EXISTS department_id INTEGER;

-- Keep old text columns as validated compatibility mirrors, never as permission keys.
CREATE OR REPLACE FUNCTION public.complaintai_sync_department()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE resolved INTEGER; expected_name TEXT;
BEGIN
  IF TG_OP='UPDATE' AND NEW.department_id IS DISTINCT FROM OLD.department_id THEN
    resolved := NEW.department_id;
  ELSIF TG_OP='UPDATE' AND NEW.department IS DISTINCT FROM OLD.department THEN
    resolved := public.complaintai_department_id(NEW.department);
    IF NEW.department IS NOT NULL AND resolved IS NULL THEN
      RAISE EXCEPTION 'Unknown department: %', NEW.department USING ERRCODE='23514';
    END IF;
  ELSE
    resolved := COALESCE(NEW.department_id, public.complaintai_department_id(NEW.department));
    IF NEW.department IS NOT NULL AND
       (resolved IS NULL OR resolved IS DISTINCT FROM public.complaintai_department_id(NEW.department)) THEN
      RAISE EXCEPTION 'Department name/ID mismatch' USING ERRCODE='23514';
    END IF;
  END IF;
  SELECT name INTO expected_name FROM public.departments WHERE id=resolved;
  IF resolved IS NOT NULL AND expected_name IS NULL THEN
    RAISE EXCEPTION 'Unknown department ID: %', resolved USING ERRCODE='23503';
  END IF;
  NEW.department_id := resolved;
  NEW.department := expected_name;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION public.complaintai_sync_complaint_department()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE resolved INTEGER; expected_name TEXT; source_name TEXT;
BEGIN
  IF TG_OP='UPDATE' AND NEW.department_id IS DISTINCT FROM OLD.department_id THEN
    resolved := NEW.department_id;
  ELSE
    IF TG_OP='UPDATE' AND NEW.category IS DISTINCT FROM OLD.category THEN
      source_name := NEW.category;
      resolved := public.complaintai_department_id(source_name);
    ELSIF TG_OP='UPDATE' AND NEW.department IS DISTINCT FROM OLD.department THEN
      source_name := NEW.department;
      resolved := public.complaintai_department_id(source_name);
    ELSE
      source_name := COALESCE(NEW.category,NEW.department);
      resolved := COALESCE(NEW.department_id,public.complaintai_department_id(source_name));
      IF source_name IS NOT NULL AND resolved IS DISTINCT FROM public.complaintai_department_id(source_name) THEN
        RAISE EXCEPTION 'Complaint department name/ID mismatch' USING ERRCODE='23514';
      END IF;
    END IF;
    IF source_name IS NOT NULL AND resolved IS NULL THEN
      RAISE EXCEPTION 'Unknown complaint category: %', source_name USING ERRCODE='23514';
    END IF;
  END IF;
  SELECT name INTO expected_name FROM public.departments WHERE id=resolved;
  IF resolved IS NOT NULL AND expected_name IS NULL THEN
    RAISE EXCEPTION 'Unknown department ID: %', resolved USING ERRCODE='23503';
  END IF;
  NEW.department_id := resolved;
  NEW.category := expected_name;
  NEW.department := expected_name;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS sync_department ON public.app_users;
CREATE TRIGGER sync_department BEFORE INSERT OR UPDATE ON public.app_users
FOR EACH ROW EXECUTE FUNCTION public.complaintai_sync_department();
DROP TRIGGER IF EXISTS sync_department ON public.complaint_responses;
CREATE TRIGGER sync_department BEFORE INSERT OR UPDATE ON public.complaint_responses
FOR EACH ROW EXECUTE FUNCTION public.complaintai_sync_department();
DROP TRIGGER IF EXISTS sync_department ON public.department_documents;
CREATE TRIGGER sync_department BEFORE INSERT OR UPDATE ON public.department_documents
FOR EACH ROW EXECUTE FUNCTION public.complaintai_sync_department();
DROP TRIGGER IF EXISTS sync_department ON public.complaints;
CREATE TRIGGER sync_department BEFORE INSERT OR UPDATE ON public.complaints
FOR EACH ROW EXECUTE FUNCTION public.complaintai_sync_complaint_department();

-- Backfill with validation; conflicting/unknown existing values abort the transaction.
UPDATE public.app_users SET department_id=COALESCE(department_id,public.complaintai_department_id(department));
UPDATE public.complaints SET department_id=COALESCE(department_id,public.complaintai_department_id(COALESCE(category,department)));
UPDATE public.complaint_responses SET department_id=COALESCE(department_id,public.complaintai_department_id(department));
UPDATE public.department_documents SET department_id=COALESCE(department_id,public.complaintai_department_id(department));

DO $$
DECLARE relation TEXT; constraint_name TEXT;
BEGIN
  FOREACH relation IN ARRAY ARRAY['app_users','complaints','complaint_responses','department_documents'] LOOP
    constraint_name := relation || '_department_id_fkey';
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=constraint_name AND conrelid=('public.'||relation)::regclass) THEN
      EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (department_id) REFERENCES public.departments(id)',relation,constraint_name);
    END IF;
  END LOOP;
END $$;
ALTER TABLE public.complaint_responses ALTER COLUMN department_id SET NOT NULL;
ALTER TABLE public.department_documents ALTER COLUMN department_id SET NOT NULL;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='app_users_department_role_check' AND conrelid='public.app_users'::regclass) THEN
    ALTER TABLE public.app_users ADD CONSTRAINT app_users_department_role_check
    CHECK ((account_role='admin' AND department_id IS NOT NULL) OR (account_role='user' AND department_id IS NULL));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS complaints_department_id_idx ON public.complaints(department_id,deleted_at,created_at DESC);
CREATE INDEX IF NOT EXISTS complaints_trash_department_idx ON public.complaints(department_id,deleted_at DESC,id DESC) WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS app_users_department_id_idx ON public.app_users(department_id);
CREATE INDEX IF NOT EXISTS complaint_responses_department_id_idx ON public.complaint_responses(department_id,complaint_id);
CREATE INDEX IF NOT EXISTS department_documents_department_id_idx ON public.department_documents(department_id,deleted_at);

COMMIT;


