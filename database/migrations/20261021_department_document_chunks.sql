-- Additive migration: do not change existing source text or short-document vectors.
ALTER TABLE rag_import_jobs ADD COLUMN IF NOT EXISTS deferred_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rag_import_jobs ADD COLUMN IF NOT EXISTS long_processed_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rag_import_jobs ADD COLUMN IF NOT EXISTS active_source_row INTEGER;
ALTER TABLE rag_import_jobs ADD COLUMN IF NOT EXISTS active_chunks_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rag_import_jobs ADD COLUMN IF NOT EXISTS active_chunks_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rag_import_failures ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'row';

CREATE TABLE IF NOT EXISTS department_documents_chunk_state (
    parent_id UUID PRIMARY KEY REFERENCES department_documents(id) ON DELETE CASCADE,
    job_id UUID REFERENCES rag_import_jobs(id) ON DELETE SET NULL,
    source_row INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'deferred' CHECK(status IN ('deferred','processing','ready','failed','cancelled')),
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    splitter_version TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    structure JSONB NOT NULL DEFAULT '[]',
    total_chunks INTEGER NOT NULL DEFAULT 0,
    completed_chunks INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS department_documents_chunk_state_job_idx ON department_documents_chunk_state(job_id,status,source_row);

CREATE TABLE IF NOT EXISTS department_documents_chunks (
    id UUID PRIMARY KEY,
    parent_id UUID NOT NULL REFERENCES department_documents_chunk_state(parent_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK(chunk_index>=0),
    start_offset INTEGER NOT NULL CHECK(start_offset>=0),
    end_offset INTEGER NOT NULL CHECK(end_offset>start_offset),
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK(token_count>0 AND token_count<=8192),
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(parent_id,chunk_index)
);
CREATE INDEX IF NOT EXISTS department_documents_chunks_embedding_hnsw_idx ON department_documents_chunks USING hnsw(embedding vector_cosine_ops);
