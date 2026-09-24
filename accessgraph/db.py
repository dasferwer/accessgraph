import os

import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init():
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(310029)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS revision (
                singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), value bigint NOT NULL);
            INSERT INTO revision VALUES (true,0) ON CONFLICT DO NOTHING;
            CREATE TABLE IF NOT EXISTS edges (
                resource text NOT NULL, relation text NOT NULL, subject text NOT NULL,
                PRIMARY KEY(resource,relation,subject));
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='edges' AND column_name='tenant') THEN
                    ALTER TABLE edges ADD COLUMN tenant text NOT NULL DEFAULT 'default';
                    ALTER TABLE edges DROP CONSTRAINT edges_pkey;
                    ALTER TABLE edges ADD PRIMARY KEY(tenant,resource,relation,subject);
                END IF;
            END $$;
            CREATE INDEX IF NOT EXISTS edge_subject ON edges(tenant,relation,subject);
            CREATE TABLE IF NOT EXISTS tenant_revision (tenant text PRIMARY KEY,value bigint NOT NULL DEFAULT 0);
            INSERT INTO tenant_revision SELECT 'default',value FROM revision ON CONFLICT DO NOTHING;
            CREATE TABLE IF NOT EXISTS audit (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,tenant text NOT NULL,
                revision bigint NOT NULL,action text NOT NULL,resource text NOT NULL,
                relation text NOT NULL,subject text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
            CREATE INDEX IF NOT EXISTS audit_tenant ON audit(tenant,id);
        """)
