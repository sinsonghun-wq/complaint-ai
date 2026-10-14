-- Additive, repeatable migration. Existing complaint data/vectors are preserved.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_sources (
    source_id TEXT PRIMARY KEY,
    metadata JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS rag_documents (
    id UUID PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES rag_sources(source_id),
    title TEXT NOT NULL,
    official_url TEXT NOT NULL,
    publisher TEXT NOT NULL,
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    jurisdiction TEXT NOT NULL DEFAULT '전국',
    effective_from DATE,
    effective_to DATE,
    retrieved_at TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL,
    original_path TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (review_status IN ('verified', 'pending')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(source_id, official_url, content_hash)
);
CREATE TABLE IF NOT EXISTS rag_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    departments TEXT[] NOT NULL DEFAULT '{}',
    type_ids TEXT[] NOT NULL DEFAULT '{}',
    locator TEXT NOT NULL,
    embedding_profile TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    UNIQUE(document_id, ordinal, embedding_profile)
);
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS applicability JSONB NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS rag_chunks_embedding_hnsw
    ON rag_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS rag_chunks_document ON rag_chunks(document_id);
CREATE INDEX IF NOT EXISTS rag_chunks_types ON rag_chunks USING gin(type_ids);

CREATE TABLE IF NOT EXISTS ai_recommendations (
    id UUID PRIMARY KEY,
    owner_user_id UUID NOT NULL,
    complaint_id BIGINT REFERENCES complaints(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','completed','failed')),
    stage TEXT NOT NULL DEFAULT 'queued',
    result JSONB,
    model_metadata JSONB NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ai_recommendations_owner ON ai_recommendations(owner_user_id,created_at DESC);
ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS conversation JSONB NOT NULL DEFAULT '[]';
ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS parent_recommendation_id UUID
    REFERENCES ai_recommendations(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS rag_import_runs (
    id UUID PRIMARY KEY,
    manifest_path TEXT NOT NULL,
    status TEXT NOT NULL,
    document_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
