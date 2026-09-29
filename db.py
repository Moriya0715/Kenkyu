"""Lightweight DB-API connector.
Provides `connect()` which returns a DB-API connection for PostgreSQL
using `DATABASE_URL`. SQLite fallback was removed to avoid accidental
local-file usage; set `DATABASE_URL` in the environment or .env.

Usage: from db import connect
    conn = connect()
    cur = conn.cursor()
"""
import os
from urllib.parse import urlparse

DB_URL = os.environ.get('DATABASE_URL')

# If DATABASE_URL not provided in environment, attempt to load simple .env file in repo root
if not DB_URL:
    dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
    try:
        if os.path.exists(dotenv_path):
            with open(dotenv_path, 'r', encoding='utf-8') as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith('#') or '=' not in ln:
                        continue
                    k, v = ln.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
            DB_URL = os.environ.get('DATABASE_URL') or os.environ.get('TOKEN_DB') or os.environ.get('FITBIT_TOKEN_DB')
            # If still missing, try to construct DATABASE_URL from POSTGRES_* vars
            if not DB_URL:
                pg_user = os.environ.get('POSTGRES_USER') or os.environ.get('POSTGRES_USERNAME')
                pg_pwd = os.environ.get('POSTGRES_PASSWORD') or os.environ.get('POSTGRES_PASS')
                pg_db = os.environ.get('POSTGRES_DB') or os.environ.get('POSTGRES_DATABASE')
                pg_host = os.environ.get('POSTGRES_HOST') or 'localhost'
                pg_port = os.environ.get('POSTGRES_PORT') or '5432'
                if pg_user and pg_pwd and pg_db:
                    constructed = f'postgresql://{pg_user}:{pg_pwd}@{pg_host}:{pg_port}/{pg_db}'
                    os.environ.setdefault('DATABASE_URL', constructed)
                    DB_URL = constructed
    except Exception:
        # don't fail import if .env can't be read
        pass


def connect():
    """Return a DB-API connection. If DB_URL is a postgres URL use psycopg2,
    otherwise fall back to sqlite3 using a file path.
    """
    if DB_URL and DB_URL.startswith(('postgres://', 'postgresql://')):
        try:
            import psycopg2
        except Exception as e:
            raise RuntimeError('psycopg2 required for Postgres connections: ' + str(e))

        # Create a psycopg2 connection and wrap it to provide a sqlite3-like
        # interface where `conn.execute(...)` returns a cursor so callers can
        # immediately call `.fetchone()` / `.fetchall()` like with sqlite3.
        conn = psycopg2.connect(DB_URL)
        # behave more like sqlite3 default autocommit/off behavior used in code
        try:
            conn.autocommit = True
        except Exception:
            pass

        class PGConnWrapper:
            def __init__(self, conn):
                self._conn = conn

            class PGCursorWrapper:
                def __init__(self, cur, parent):
                    self._cur = cur
                    self._parent = parent

                def execute(self, query, params=None):
                    if params is not None and '?' in query:
                        query = query.replace('?', '%s')
                    if params is None:
                        return self._cur.execute(query)
                    return self._cur.execute(query, params)

                def fetchone(self):
                    return self._cur.fetchone()

                def fetchall(self):
                    return self._cur.fetchall()

                def __getattr__(self, name):
                    return getattr(self._cur, name)

            def cursor(self):
                # return a cursor-like wrapper that normalizes '?' placeholders
                cur = self._conn.cursor()
                return PGConnWrapper.PGCursorWrapper(cur, self)

            def execute(self, query, params=None):
                cur = self._conn.cursor()
                # psycopg2 uses %s placeholders; many modules use sqlite-style '?'
                if params is not None and '?' in query:
                    query = query.replace('?', '%s')
                if params is None:
                    cur.execute(query)
                else:
                    cur.execute(query, params)
                try:
                    # attempt to fetch to detect result availability
                    # but do not consume cursor for callers; just return cursor
                    return cur
                except Exception:
                    return cur

            def commit(self):
                try:
                    self._conn.commit()
                except Exception:
                    pass

            def close(self):
                try:
                    self._conn.close()
                except Exception:
                    pass

        return PGConnWrapper(conn)
    # Do not fall back to SQLite. Require DATABASE_URL for runtime.
    raise RuntimeError('DATABASE_URL must be set to a Postgres URL. SQLite fallback removed.')
