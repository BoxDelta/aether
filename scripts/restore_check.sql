-- Restore check (docs/runbooks/db-backup-restore.md, ADR-025). Run as the owner
-- against a restored database; prints exactly "ok" when the append-only triggers
-- and the least-privilege rights (migration 0003) are all in place, else "NOT OK"
-- and the failing checks. Reads only.
WITH c(name, pass) AS (VALUES
    ('triggers', (SELECT count(*) FROM pg_trigger
                  WHERE tgrelid = 'events'::regclass AND NOT tgisinternal
                    AND tgname IN ('events_no_update_delete', 'events_no_truncate')) = 2),
    ('app_insert_select', has_table_privilege('aether_app', 'events', 'INSERT, SELECT')
                          AND has_table_privilege('aether_app', 'alembic_version', 'SELECT')),
    ('app_no_change', NOT has_table_privilege('aether_app', 'events',
                                              'UPDATE, DELETE, TRUNCATE, TRIGGER')),
    ('app_connect', has_database_privilege('aether_app', current_database(), 'CONNECT')),
    ('public_table', NOT has_table_privilege('public', 'events', 'SELECT, INSERT')),
    ('public_schema', NOT has_schema_privilege('public', 'public', 'USAGE, CREATE')),
    ('public_function', NOT has_function_privilege('public', 'events_append_only()', 'EXECUTE')),
    ('public_database', NOT has_database_privilege('public', current_database(),
                                                   'CONNECT, TEMPORARY')),
    -- 0003: functions the owner creates later get no PUBLIC EXECUTE by default.
    ('default_function_acl', EXISTS (
        SELECT 1 FROM pg_default_acl d
        WHERE d.defaclobjtype = 'f' AND d.defaclnamespace = 0
          AND d.defaclrole = (SELECT relowner FROM pg_class WHERE oid = 'events'::regclass)
          AND NOT EXISTS (SELECT 1 FROM aclexplode(d.defaclacl) a
                          WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')))
)
SELECT CASE WHEN bool_and(pass) THEN 'ok'
            ELSE 'NOT OK: ' || string_agg(name, ', ') FILTER (WHERE NOT pass) END
FROM c;
