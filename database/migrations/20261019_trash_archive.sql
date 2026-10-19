-- Additive, repeatable migration. Does not permanently delete complaints.
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS original_owner_user_id UUID REFERENCES app_users(owner_id) ON DELETE SET NULL;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS trash_previous_status TEXT;

-- Older administrator cancellations were hidden in the active archive rather
-- than moved to trash. Keep their original content and cancellation information.
UPDATE complaints SET deleted_at=COALESCE(cancelled_at,status_updated_at,created_at),
    original_owner_user_id=COALESCE(original_owner_user_id,owner_user_id),
    analysis_state='cancelled',analysis_revision=analysis_revision+1
WHERE complaint_status='취소' AND deleted_at IS NULL;
UPDATE complaints SET original_owner_user_id=owner_user_id
WHERE deleted_at IS NOT NULL AND original_owner_user_id IS NULL AND owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS complaints_trash_time_idx ON complaints(deleted_at DESC,id DESC) WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS complaints_trash_department_idx ON complaints(department_id,deleted_at DESC,id DESC) WHERE deleted_at IS NOT NULL;
