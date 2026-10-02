-- ComplaintAI 개발 DB 재생성 스크립트 (PostgreSQL 16 + pgvector)
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
  embedding vector(1536),
  source_file TEXT,
  source_row INTEGER,
  content_fingerprint CHAR(64),
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
  previous_context JSONB
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
CREATE INDEX complaints_content_fingerprint_idx ON complaints(content_fingerprint);
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
  embedding vector(1536),
  created_by UUID NOT NULL REFERENCES app_users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX department_documents_version_idx ON department_documents(document_id, version);
CREATE INDEX department_documents_scope_idx ON department_documents(department, deleted_at, created_at DESC);
CREATE INDEX department_documents_embedding_hnsw_idx ON department_documents USING hnsw (embedding vector_cosine_ops);

INSERT INTO app_users (id, owner_id, email, username, display_name, password_salt, password_hash, account_role, department) VALUES
  ('00000000-0000-4000-8000-000000000001', '00000000-0000-4000-9000-000000000001', 'user-a@complaintai.local', 'user-a', '테스트 일반 사용자 A', '58bf4559c2c4498063d07dd64de7c61d', '8f0d4903c225b01684ab6d46548c193b6f549dd9dbbd14193e6c1e2a7a2b681c15316783cb9962c3befdca1a0dcf987ade02784318547b9350ef62bac4d161ee', 'user', NULL),
  ('00000000-0000-4000-8000-000000000002', '00000000-0000-4000-9000-000000000002', 'user-b@complaintai.local', 'user-b', '테스트 일반 사용자 B', '120b0ba4daf51a304f2c743204ba0d17', '3cbe0ecd7334a8b751c9eab368158eba290c6ac8c6199b7148eb7bcf6406701d14c10a7d50874f079ab8245c47805cf2fe93f847df43c7c43967e2cbff037f1a', 'user', NULL),
  ('00000000-0000-4000-8000-000000000101', '00000000-0000-4000-9000-000000000101', 'admin-administration-safety@complaintai.local', 'admin-administration-safety', '행정·안전 관리자', '3a38b03359f26a8e9e3bba75cec5dcc6', '04624396c44e9e14a767325dd09d21686e4e94398ed1e0d8ee4289a37c23811e51401023f25f4fb0afc30bf60f6edec4d9f870659227daa5e7ce6db11bfaa721', 'admin', '행정·안전'),
  ('00000000-0000-4000-8000-000000000102', '00000000-0000-4000-9000-000000000102', 'admin-land-transport@complaintai.local', 'admin-land-transport', '국토·교통 관리자', '29f49778b76d123bd7001754fdf9995f', '212d95ba99eff11252f1e18817568689e59e1f71e4cdc8e0d258a3a3a88e6badc041dab5e2dda6711df3742f7b0f38999f051905b2c5219aff6d670411b00471', 'admin', '국토·교통'),
  ('00000000-0000-4000-8000-000000000103', '00000000-0000-4000-9000-000000000103', 'admin-housing@complaintai.local', 'admin-housing', '주택건축 관리자', 'b4c8b8c257a7bf844a13aaed1d8b6462', '0b977081db25891d3a11ceae60adc0d6f9bb9f2b58cd21c968fa7048667dc54ad9c1951988e9a96639636441484656795070e619946ea7b840636d08c382acd6', 'admin', '주택건축'),
  ('00000000-0000-4000-8000-000000000104', '00000000-0000-4000-9000-000000000104', 'admin-environment@complaintai.local', 'admin-environment', '환경·위생 관리자', '9e030b1888427dc21e93955b3f2888a1', 'ae3b6ccd31aed6a159997b247b8601287d9fb0feca1744f119bb539ee5304707d05461a2b16cffb6594ea4c2988fd25f49374f0d8c0e1610e7310546f24eff2a', 'admin', '환경·위생'),
  ('00000000-0000-4000-8000-000000000105', '00000000-0000-4000-9000-000000000105', 'admin-welfare@complaintai.local', 'admin-welfare', '보건복지 관리자', '340a77c8eaa83510b012897f8ea37f5d', '4a4f31ecf6bf15a6cbc6cd149746127ae83ca09f35454a48e8169147f2a3ec8c3c8886388b4670ed8035b8fde25d8dd7e30f72ab6a1a6cfa10eb7ddd145da983', 'admin', '보건복지'),
  ('00000000-0000-4000-8000-000000000106', '00000000-0000-4000-9000-000000000106', 'admin-fire@complaintai.local', 'admin-fire', '소방 관리자', 'bdcf982b99c2c8ccd14bd305629d85f4', '809f23171ae066decb9c885c2bf64042ed07f984ff0d3b07c5c124493edbfc539d50f71d960b8c1889408745c0ac0745a4f7285f17f61ae04775f9fc3f32a155', 'admin', '소방'),
  ('00000000-0000-4000-8000-000000000107', '00000000-0000-4000-9000-000000000107', 'admin-other@complaintai.local', 'admin-other', '기타 관리자', 'cf40bab759cc5f6a387d4178a8e364c5', 'cac10762bc2ad4146b6f89b6d3d0cf2d86cbe69d119bd5c37e8c3c5ad5b2daba6811243d74c00b5bf47f3bd4b87c99e8a54ad4916fc43457586fa1e534727e2a', 'admin', '기타')
ON CONFLICT (username) DO NOTHING;

COMMIT;


