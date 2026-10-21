-- Recreate the original empty table after an accidental DROP.
-- This restores schema only; it does not restore documents or resume imports.
BEGIN;

CREATE TABLE public.department_documents (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL,
    department_id INTEGER NOT NULL REFERENCES public.departments(id),
    title TEXT NOT NULL,
    original_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding public.vector(1024),
    created_by UUID NOT NULL REFERENCES public.app_users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX department_documents_version_idx
    ON public.department_documents(document_id, version);
CREATE INDEX department_documents_scope_idx
    ON public.department_documents(department_id, deleted_at, created_at DESC);
CREATE INDEX department_documents_embedding_hnsw_idx
    ON public.department_documents USING hnsw (embedding public.vector_cosine_ops);
CREATE INDEX department_documents_content_lookup_idx
    ON public.department_documents((md5(content)));

COMMIT;
