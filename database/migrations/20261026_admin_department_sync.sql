-- Additive and repeatable. Existing numeric assignments always take precedence.
-- Repair only the known legacy accounts whose department assignment is missing.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(hashtext('complaintai.admin_department_sync'));

UPDATE public.app_users AS u
SET department_id = d.id
FROM (VALUES
    ('admin-administration-safety', '문화·행정·안전'),
    ('admin-land-transport', '교통·국토'),
    ('admin-housing', '주택·건축'),
    ('admin-environment', '환경·위생'),
    ('admin-welfare', '보건·복지'),
    ('admin-fire', '노동·기업'),
    ('admin-other', '기타')
) AS legacy(username, department_name)
JOIN public.departments AS d ON d.name = legacy.department_name
WHERE u.username = legacy.username AND u.account_role = 'admin'
    AND u.department_id IS NULL;

CREATE OR REPLACE FUNCTION public.complaintai_admin_department_identity()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE current_name TEXT;
BEGIN
    IF NEW.account_role = 'admin' AND NEW.department_id IS NOT NULL THEN
        SELECT name INTO current_name FROM public.departments WHERE id = NEW.department_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Unknown administrator department ID: %', NEW.department_id
                USING ERRCODE = '23503';
        END IF;
        NEW.display_name := current_name || ' 관리자';
    END IF;
    RETURN NEW;
END $$;

-- Runs after the optional legacy sync_department BEFORE trigger.
CREATE OR REPLACE TRIGGER sync_user_department_identity
BEFORE INSERT OR UPDATE OF department_id, account_role, display_name ON public.app_users
FOR EACH ROW EXECUTE FUNCTION public.complaintai_admin_department_identity();

CREATE OR REPLACE FUNCTION public.complaintai_rename_department_admins()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    UPDATE public.app_users SET display_name = NEW.name || ' 관리자'
    WHERE account_role = 'admin' AND department_id = NEW.id
        AND display_name IS DISTINCT FROM NEW.name || ' 관리자';
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER sync_department_admin_names
AFTER UPDATE OF name ON public.departments
FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name)
EXECUTE FUNCTION public.complaintai_rename_department_admins();

UPDATE public.app_users AS u SET display_name = d.name || ' 관리자'
FROM public.departments AS d
WHERE u.account_role = 'admin' AND u.department_id = d.id
    AND u.display_name IS DISTINCT FROM d.name || ' 관리자';

-- Reject new unassigned administrators rather than accepting an unusable account.
-- Unknown legacy NULL assignments stay read-only; never guess their department.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.app_users'::regclass
            AND conname = 'app_users_admin_department_required') THEN
        ALTER TABLE public.app_users ADD CONSTRAINT app_users_admin_department_required
            CHECK (account_role <> 'admin' OR department_id IS NOT NULL) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.app_users
        WHERE account_role = 'admin' AND department_id IS NULL) THEN
        ALTER TABLE public.app_users VALIDATE CONSTRAINT app_users_admin_department_required;
    END IF;
END $$;
