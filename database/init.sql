-- ComplaintAI PostgreSQL 초기 스키마
-- Docker의 빈 postgres_data 볼륨에서 한 번 실행된다.
-- 기존 DB에 수동 실행해도 테이블·컬럼·인덱스 생성은 안전하게 반복할 수 있다.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS app_users (
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

CREATE TABLE IF NOT EXISTS complaints (
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
  deleted_at TIMESTAMPTZ
);

-- 업무상 취소는 보관함 삭제와 구분하여 기록한다. 재민원은 이전 내용을 스냅샷으로 보존한다.
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS cancelled_by_role TEXT;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS cancellation_reason TEXT;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS parent_complaint_id BIGINT REFERENCES complaints(id) ON DELETE SET NULL;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS previous_context JSONB;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS analysis_state TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS analysis_revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS source_files (
  id UUID PRIMARY KEY,
  original_name TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  mime_type TEXT,
  size_bytes BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retained_until TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS import_jobs (
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

CREATE TABLE IF NOT EXISTS csv_schema_mappings (
  id BIGSERIAL PRIMARY KEY,
  schema_signature CHAR(64) NOT NULL UNIQUE,
  profile_name TEXT,
  column_mapping JSONB NOT NULL,
  confidence NUMERIC(3,2) NOT NULL DEFAULT 0,
  created_by_user_id UUID REFERENCES app_users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS import_failures (
  id BIGSERIAL PRIMARY KEY,
  job_id UUID NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
  source_row INTEGER NOT NULL,
  raw_data JSONB NOT NULL,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS complaint_responses (
  id UUID PRIMARY KEY,
  complaint_id BIGINT NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
  author_user_id UUID NOT NULL REFERENCES app_users(id),
  department TEXT NOT NULL,
  content TEXT NOT NULL,
  response_state TEXT NOT NULL DEFAULT 'sent' CHECK (response_state IN ('draft', 'sent')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sent_at TIMESTAMPTZ
);

-- 이전의 최소 complaints 스키마를 최신 FastAPI 스키마로 보완한다.
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS source_file TEXT;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS source_row INTEGER;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS content_fingerprint CHAR(64);
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS processing_mode TEXT NOT NULL DEFAULT 'fallback';
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS llm_model TEXT;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS prompt_version TEXT;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS analysis_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS owner_user_id UUID;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS complaint_status TEXT NOT NULL DEFAULT '접수';
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS column_mapping JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS schema_signature CHAR(64);
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS checkpoint_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE import_jobs DROP CONSTRAINT IF EXISTS import_jobs_status_check;
ALTER TABLE import_jobs ADD CONSTRAINT import_jobs_status_check CHECK (status IN ('awaiting_mapping', 'queued', 'processing', 'completed', 'failed'));
ALTER TABLE complaint_responses ADD COLUMN IF NOT EXISTS response_state TEXT NOT NULL DEFAULT 'sent';
ALTER TABLE complaint_responses ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS app_users_owner_id_idx ON app_users(owner_id);
CREATE INDEX IF NOT EXISTS complaints_created_at_idx ON complaints(created_at DESC);
CREATE INDEX IF NOT EXISTS complaints_owner_idx ON complaints(owner_user_id, deleted_at);
CREATE INDEX IF NOT EXISTS complaints_deleted_at_idx ON complaints(deleted_at);
CREATE INDEX IF NOT EXISTS complaints_content_fingerprint_idx ON complaints(content_fingerprint);
CREATE INDEX IF NOT EXISTS complaints_embedding_hnsw_idx ON complaints USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS import_jobs_owner_idx ON import_jobs(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS import_jobs_queue_idx ON import_jobs(status, created_at) WHERE status IN ('queued', 'processing');
CREATE INDEX IF NOT EXISTS import_failures_job_idx ON import_failures(job_id, source_row);
CREATE INDEX IF NOT EXISTS csv_schema_mappings_signature_idx ON csv_schema_mappings(schema_signature);
CREATE INDEX IF NOT EXISTS complaint_responses_complaint_idx ON complaint_responses(complaint_id, created_at);

-- local 개발·테스트 전용 초기 계정. 운영 환경에서는 반드시 삭제하거나 별도 비밀번호로 교체한다.
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

