CREATE TABLE IF NOT EXISTS rag_import_jobs (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL,
    source_file TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    encoding TEXT NOT NULL,
    column_mapping JSONB NOT NULL,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    created_by UUID NOT NULL REFERENCES app_users(id),
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','processing','completed','cancelled','failed')),
    stage TEXT NOT NULL DEFAULT 'queued',
    total_rows INTEGER NOT NULL DEFAULT 0,
    completed_rows INTEGER NOT NULL DEFAULT 0,
    checkpoint_rows INTEGER NOT NULL DEFAULT 0,
    saved_rows INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0,
    failed_rows INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT NOT NULL,
    last_error TEXT,
    worker_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS rag_import_jobs_owner_idx ON rag_import_jobs(created_by,created_at DESC);
CREATE INDEX IF NOT EXISTS rag_import_jobs_queue_idx ON rag_import_jobs(created_at) WHERE status='queued';
CREATE TABLE IF NOT EXISTS rag_import_failures (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES rag_import_jobs(id) ON DELETE CASCADE,
    source_row INTEGER NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rag_import_failures_job_idx ON rag_import_failures(job_id,source_row);
-- Hash narrows the lookup; equality is still checked against the FULL content.
-- Existing duplicates are preserved. New imports never normalize or truncate text.
CREATE INDEX IF NOT EXISTS department_documents_content_lookup_idx ON department_documents((md5(content)));
