-- Restore check (docs/runbooks/db-backup-restore.md, ADR-025). Run as the owner
-- against a restored database; prints exactly "ok" when the append-only triggers
-- (present and enabled)
-- and the least-privilege rights (migrations 0003 and 0004) are all in place,
-- else "NOT OK" and the failing checks. Reads only.
-- Objects are named with their schema (public), as the migrations create them.
-- has_table_privilege() with a list is true if ANY of the rights is held, so rights
-- that must all be present are checked one by one.
WITH c(name, pass) AS (VALUES
    ('triggers', (SELECT count(*) FROM pg_trigger
                  WHERE tgrelid = 'public.events'::regclass AND NOT tgisinternal
                    AND tgenabled IN ('O', 'A')
                    AND tgname IN ('events_no_update_delete', 'events_no_truncate')) = 2),
    -- 0007: only the owner may write actor 'restore'. A backup from before 0007
    -- passes; the repair's upgrade to head adds the trigger.
    ('restore_actor_trigger', (SELECT version_num FROM public.alembic_version) < '0007'
                              OR EXISTS (SELECT 1 FROM pg_trigger
                                         WHERE tgrelid = 'public.events'::regclass
                                           AND tgname = 'events_restore_actor_owner_only'
                                           AND tgenabled IN ('O', 'A'))),
    ('app_insert_select', has_table_privilege('aether_app', 'public.events', 'INSERT')
                          AND has_table_privilege('aether_app', 'public.events', 'SELECT')
                          AND has_table_privilege('aether_app', 'public.alembic_version', 'SELECT')),
    ('app_no_change', NOT has_table_privilege('aether_app', 'public.events',
                                              'UPDATE, DELETE, TRUNCATE, TRIGGER')
                      AND NOT has_any_column_privilege('aether_app', 'public.events', 'UPDATE')
                      AND NOT has_table_privilege('aether_app', 'public.alembic_version',
                                                  'INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER')),
    ('app_connect', has_database_privilege('aether_app', current_database(), 'CONNECT')),
    ('public_table', NOT has_table_privilege('public', 'public.events',
                     'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
                     AND NOT has_any_column_privilege('public', 'public.events',
                                                      'SELECT, INSERT, UPDATE, REFERENCES')),
    ('public_schema', NOT has_schema_privilege('public', 'public', 'USAGE, CREATE')),
    ('public_function', NOT has_function_privilege('public', 'public.events_append_only()', 'EXECUTE')),
    ('public_database', NOT has_database_privilege('public', current_database(),
                                                   'CONNECT, TEMPORARY')),
    -- 0003: functions the owner creates later get no PUBLIC EXECUTE by default.
    ('default_function_acl', EXISTS (
        SELECT 1 FROM pg_default_acl d
        WHERE d.defaclobjtype = 'f' AND d.defaclnamespace = 0
          AND d.defaclrole = (SELECT relowner FROM pg_class WHERE oid = 'public.events'::regclass)
          AND NOT EXISTS (SELECT 1 FROM aclexplode(d.defaclacl) a
                          WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'))),
    -- 0004: memory_records. A backup taken before 0004 has no such table: only
    -- memory_table fails (runbook R4).
    ('memory_table', to_regclass('public.memory_records') IS NOT NULL),
    ('memory_app', to_regclass('public.memory_records') IS NULL OR (
        has_table_privilege('aether_app', to_regclass('public.memory_records'), 'SELECT')
        AND has_table_privilege('aether_app', to_regclass('public.memory_records'), 'INSERT')
        AND has_table_privilege('aether_app', to_regclass('public.memory_records'), 'UPDATE')
        AND has_table_privilege('aether_app', to_regclass('public.memory_records'), 'DELETE')
        AND NOT has_table_privilege('aether_app', to_regclass('public.memory_records'),
                                    'TRUNCATE, TRIGGER, REFERENCES'))),
    ('memory_public', to_regclass('public.memory_records') IS NULL OR NOT has_table_privilege(
        'public', to_regclass('public.memory_records'),
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
        AND NOT has_any_column_privilege('public', to_regclass('public.memory_records'),
                                         'SELECT, INSERT, UPDATE, REFERENCES')),
    ('memory_owner', to_regclass('public.memory_records') IS NULL OR NOT pg_has_role(
        'aether_app', (SELECT relowner FROM pg_class WHERE oid = to_regclass('public.memory_records')),
        'MEMBER'))
)
SELECT CASE WHEN bool_and(pass) THEN 'ok'
            ELSE 'NOT OK: ' || string_agg(name, ', ') FILTER (WHERE NOT pass) END
FROM c;
