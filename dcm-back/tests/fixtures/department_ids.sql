BEGIN;
SET LOCAL lock_timeout='10s';
-- Stable department IDs. Additive and repeatable; never resets account/complaint data.
CREATE TABLE IF NOT EXISTS public.departments (
  id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 7),
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL UNIQUE,
  aliases TEXT[] NOT NULL DEFAULT '{}'
);
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.departments d JOIN (VALUES
    (1, 'labor_business', '노동·기업'),
    (2, 'traffic_land', '교통·국토'),
    (3, 'housing', '주택·건축'),
    (4, 'environment', '환경·위생'),
    (5, 'culture_admin_safety', '문화·행정·안전'),
    (6, 'welfare', '보건·복지'),
    (7, 'etc', '기타')
  ) AS expected(id,code,name) ON expected.id=d.id
  WHERE d.code<>expected.code OR d.name<>expected.name) THEN
    RAISE EXCEPTION 'Department IDs conflict with the fixed catalog; migration aborted';
  END IF;
END $$;
INSERT INTO public.departments(id,code,name,aliases) VALUES
  (1, 'labor_business', '노동·기업', ARRAY['노동','기업']::text[]),
  (2, 'traffic_land', '교통·국토', ARRAY['교통','건설·국토','국토·교통','국토교통']::text[]),
  (3, 'housing', '주택·건축', ARRAY['주택건축']::text[]),
  (4, 'environment', '환경·위생', ARRAY[]::text[]),
  (5, 'culture_admin_safety', '문화·행정·안전', ARRAY['행정·안전','행정안전','소방']::text[]),
  (6, 'welfare', '보건·복지', ARRAY['보건복지']::text[]),
  (7, 'etc', '기타', ARRAY[]::text[])
ON CONFLICT (id) DO NOTHING;

CREATE OR REPLACE FUNCTION public.complaintai_department_id(value TEXT)
RETURNS INTEGER LANGUAGE SQL STABLE AS $$
  SELECT id FROM public.departments
  WHERE name=BTRIM(value) OR BTRIM(value)=ANY(aliases) OR id::text=BTRIM(value)
$$;

ALTER TABLE public.app_users ADD COLUMN IF NOT EXISTS department_id INTEGER;
ALTER TABLE public.complaints ADD COLUMN IF NOT EXISTS department_id INTEGER;
ALTER TABLE public.complaint_responses ADD COLUMN IF NOT EXISTS department_id INTEGER;
ALTER TABLE public.department_documents ADD COLUMN IF NOT EXISTS department_id INTEGER;

-- Keep old text columns as validated compatibility mirrors, never as permission keys.
CREATE OR REPLACE FUNCTION public.complaintai_sync_department()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE resolved INTEGER; expected_name TEXT;
BEGIN
  IF TG_OP='UPDATE' AND NEW.department_id IS DISTINCT FROM OLD.department_id THEN
    resolved := NEW.department_id;
  ELSIF TG_OP='UPDATE' AND NEW.department IS DISTINCT FROM OLD.department THEN
    resolved := public.complaintai_department_id(NEW.department);
    IF NEW.department IS NOT NULL AND resolved IS NULL THEN
      RAISE EXCEPTION 'Unknown department: %', NEW.department USING ERRCODE='23514';
    END IF;
  ELSE
    resolved := COALESCE(NEW.department_id, public.complaintai_department_id(NEW.department));
    IF NEW.department IS NOT NULL AND
       (resolved IS NULL OR resolved IS DISTINCT FROM public.complaintai_department_id(NEW.department)) THEN
      RAISE EXCEPTION 'Department name/ID mismatch' USING ERRCODE='23514';
    END IF;
  END IF;
  SELECT name INTO expected_name FROM public.departments WHERE id=resolved;
  IF resolved IS NOT NULL AND expected_name IS NULL THEN
    RAISE EXCEPTION 'Unknown department ID: %', resolved USING ERRCODE='23503';
  END IF;
  NEW.department_id := resolved;
  NEW.department := expected_name;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION public.complaintai_sync_complaint_department()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE resolved INTEGER; expected_name TEXT; source_name TEXT;
BEGIN
  IF TG_OP='UPDATE' AND NEW.department_id IS DISTINCT FROM OLD.department_id THEN
    resolved := NEW.department_id;
  ELSE
    IF TG_OP='UPDATE' AND NEW.category IS DISTINCT FROM OLD.category THEN
      source_name := NEW.category;
      resolved := public.complaintai_department_id(source_name);
    ELSIF TG_OP='UPDATE' AND NEW.department IS DISTINCT FROM OLD.department THEN
      source_name := NEW.department;
      resolved := public.complaintai_department_id(source_name);
    ELSE
      source_name := COALESCE(NEW.category,NEW.department);
      resolved := COALESCE(NEW.department_id,public.complaintai_department_id(source_name));
      IF source_name IS NOT NULL AND resolved IS DISTINCT FROM public.complaintai_department_id(source_name) THEN
        RAISE EXCEPTION 'Complaint department name/ID mismatch' USING ERRCODE='23514';
      END IF;
    END IF;
    IF source_name IS NOT NULL AND resolved IS NULL THEN
      RAISE EXCEPTION 'Unknown complaint category: %', source_name USING ERRCODE='23514';
    END IF;
  END IF;
  SELECT name INTO expected_name FROM public.departments WHERE id=resolved;
  IF resolved IS NOT NULL AND expected_name IS NULL THEN
    RAISE EXCEPTION 'Unknown department ID: %', resolved USING ERRCODE='23503';
  END IF;
  NEW.department_id := resolved;
  NEW.category := expected_name;
  NEW.department := expected_name;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS sync_department ON public.app_users;
CREATE TRIGGER sync_department BEFORE INSERT OR UPDATE ON public.app_users
FOR EACH ROW EXECUTE FUNCTION public.complaintai_sync_department();
DROP TRIGGER IF EXISTS sync_department ON public.complaint_responses;
CREATE TRIGGER sync_department BEFORE INSERT OR UPDATE ON public.complaint_responses
FOR EACH ROW EXECUTE FUNCTION public.complaintai_sync_department();
DROP TRIGGER IF EXISTS sync_department ON public.department_documents;
CREATE TRIGGER sync_department BEFORE INSERT OR UPDATE ON public.department_documents
FOR EACH ROW EXECUTE FUNCTION public.complaintai_sync_department();
DROP TRIGGER IF EXISTS sync_department ON public.complaints;
CREATE TRIGGER sync_department BEFORE INSERT OR UPDATE ON public.complaints
FOR EACH ROW EXECUTE FUNCTION public.complaintai_sync_complaint_department();

-- Backfill with validation; conflicting/unknown existing values abort the transaction.
UPDATE public.app_users SET department_id=COALESCE(department_id,public.complaintai_department_id(department));
UPDATE public.complaints SET department_id=COALESCE(department_id,public.complaintai_department_id(COALESCE(category,department)));
UPDATE public.complaint_responses SET department_id=COALESCE(department_id,public.complaintai_department_id(department));
UPDATE public.department_documents SET department_id=COALESCE(department_id,public.complaintai_department_id(department));

DO $$
DECLARE relation TEXT; constraint_name TEXT;
BEGIN
  FOREACH relation IN ARRAY ARRAY['app_users','complaints','complaint_responses','department_documents'] LOOP
    constraint_name := relation || '_department_id_fkey';
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=constraint_name AND conrelid=('public.'||relation)::regclass) THEN
      EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (department_id) REFERENCES public.departments(id)',relation,constraint_name);
    END IF;
  END LOOP;
END $$;
ALTER TABLE public.complaint_responses ALTER COLUMN department_id SET NOT NULL;
ALTER TABLE public.department_documents ALTER COLUMN department_id SET NOT NULL;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='app_users_department_role_check' AND conrelid='public.app_users'::regclass) THEN
    ALTER TABLE public.app_users ADD CONSTRAINT app_users_department_role_check
    CHECK ((account_role='admin' AND department_id IS NOT NULL) OR (account_role='user' AND department_id IS NULL));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS complaints_department_id_idx ON public.complaints(department_id,deleted_at,created_at DESC);
CREATE INDEX IF NOT EXISTS app_users_department_id_idx ON public.app_users(department_id);
CREATE INDEX IF NOT EXISTS complaint_responses_department_id_idx ON public.complaint_responses(department_id,complaint_id);
CREATE INDEX IF NOT EXISTS department_documents_department_id_idx ON public.department_documents(department_id,deleted_at);
COMMIT;
