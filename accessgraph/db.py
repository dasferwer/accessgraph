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
        """)
