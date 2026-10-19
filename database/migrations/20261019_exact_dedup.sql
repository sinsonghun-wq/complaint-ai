-- Additive; existing duplicates are preserved, masked PII is never reconstructed.
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS raw_content_fingerprint CHAR(64);
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS dedup_compact_fingerprint CHAR(64);
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS dedup_profile JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS complaints_dedup_candidates_idx
    ON complaints(owner_user_id,dedup_compact_fingerprint)
    WHERE deleted_at IS NULL AND complaint_status<>'취소';
CREATE UNIQUE INDEX IF NOT EXISTS complaints_active_raw_unique
    ON complaints(owner_user_id,raw_content_fingerprint) NULLS NOT DISTINCT
    WHERE raw_content_fingerprint IS NOT NULL AND deleted_at IS NULL AND complaint_status<>'취소';
