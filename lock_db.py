"""DB-backed named locks (Postgres `locks` table) with TTL + heartbeat."""
from __future__ import annotations

import logging
import os
import socket
import threading
import uuid

import db

TTL_SECONDS = 60
HEARTBEAT_INTERVAL = 10


def make_owner() -> str:
    return f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'


def ensure_table(conn) -> None:
    cur = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name='locks' AND column_name='lock_name'"
    )
    if cur.fetchone():
        return
    # Legacy `locks` (user_id PK) was unused; replace it.
    conn.execute('DROP TABLE IF EXISTS locks')
    conn.execute(
        """CREATE TABLE IF NOT EXISTS locks (
            lock_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )


def acquire(lock_name: str, owner: str, ttl: int = TTL_SECONDS) -> bool:
    """Return True if the lock was taken (free, expired, or already ours)."""
    conn = db.connect()
    try:
        ensure_table(conn)
        cur = conn.execute(
            """INSERT INTO locks (lock_name, owner, acquired_at, heartbeat_at)
               VALUES (%s, %s, now(), now())
               ON CONFLICT (lock_name) DO UPDATE
                 SET owner = EXCLUDED.owner, acquired_at = now(), heartbeat_at = now()
                 WHERE locks.heartbeat_at < now() - make_interval(secs => %s)
                    OR locks.owner = EXCLUDED.owner
               RETURNING owner""",
            (lock_name, owner, ttl),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def release(lock_name: str, owner: str) -> None:
    try:
        conn = db.connect()
        try:
            conn.execute('DELETE FROM locks WHERE lock_name = %s AND owner = %s', (lock_name, owner))
        finally:
            conn.close()
    except Exception:
        logging.exception('lock_db: release failed for %s', lock_name)


def is_held(lock_name: str, ttl: int = TTL_SECONDS) -> bool:
    """True if a live (heartbeat within TTL) lock exists. False on any DB error."""
    try:
        conn = db.connect()
        try:
            cur = conn.execute(
                'SELECT 1 FROM locks WHERE lock_name = %s AND heartbeat_at > now() - make_interval(secs => %s)',
                (lock_name, ttl),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


def start_heartbeat(lock_name: str, owner: str, interval: int = HEARTBEAT_INTERVAL) -> threading.Event:
    """Refresh heartbeat_at in a daemon thread. Set the returned event to stop."""
    stop = threading.Event()

    def _loop():
        while not stop.wait(interval):
            try:
                conn = db.connect()
                try:
                    cur = conn.execute(
                        'UPDATE locks SET heartbeat_at = now() WHERE lock_name = %s AND owner = %s',
                        (lock_name, owner),
                    )
                    if cur.rowcount == 0:
                        logging.warning('lock_db: lock %s no longer owned by %s', lock_name, owner)
                finally:
                    conn.close()
            except Exception:
                logging.exception('lock_db: heartbeat failed for %s', lock_name)

    threading.Thread(target=_loop, name=f'lock-heartbeat-{lock_name}', daemon=True).start()
    return stop
