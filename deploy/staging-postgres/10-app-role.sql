-- Application connections cannot administer roles or bypass PostgreSQL permissions.
DO $$
BEGIN
    EXECUTE format(
        'CREATE ROLE saas_staging LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L',
        trim(pg_read_file('/run/secrets/staging_app_password'))
    );
END
$$;
ALTER DATABASE saas_staging OWNER TO saas_staging;
ALTER SCHEMA public OWNER TO saas_staging;
