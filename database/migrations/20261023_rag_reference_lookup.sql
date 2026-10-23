-- Equality lookup without indexing unbounded title text; original rows stay intact.
CREATE INDEX IF NOT EXISTS department_documents_title_lookup_idx
    ON department_documents (md5(title)) WHERE deleted_at IS NULL;
