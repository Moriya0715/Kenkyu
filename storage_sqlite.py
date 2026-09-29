"""storage_sqlite.py

Helpers to manage SQLite-backed index and atomic JSON writes.
- ensures schema in `tokens.db` (by default)
- append_sample_json(user_id, dt, sample): writes to JSON with file lock
- insert_sample_db(user_id, dt, sample): inserts into samples table
- enqueue_notification(user_id, channel, text)
- pop_pending_notifications(limit)

This module will transparently use Postgres when `DATABASE_URL` is set to a
Postgres URL; in that case connections are obtained via `db.connect()` and
Postgres-specific SQL paths are taken where necessary. Otherwise the module
behaves as the original sqlite-backed implementation.
"""
import logging
import os
import time
import json
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any

# optional dependency for file locking
try:
    import portalocker
except Exception:
    portalocker = None
    try:
        print('Warning: optional package "portalocker" is not installed. storage_sqlite will use in-process locks; for cross-process locking install: pip install portalocker')
    except Exception:
        pass
import threading

# in-process lock map used as a fallback when portalocker cannot lock within same process
_inproc_locks: Dict[str, threading.Lock] = {}
_inproc_locks_lock = threading.Lock()

DB_PATH = os.environ.get('DATABASE_URL')
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCKS_DIR = os.path.join(REPO_ROOT, 'locks')

# Helper to detect Postgres and obtain connections via central `db` wrapper
def _is_postgres_url() -> bool:
    try:
        from db import DB_URL
        return bool(DB_URL and DB_URL.startswith(('postgres://', 'postgresql://')))
    except Exception:
        return False


def _get_conn(path: str = DB_PATH):
    """Return a Postgres connection via `db.connect()`.

    This module is converted to Postgres-only. If connecting to Postgres
    fails, raise an error so the caller can detect misconfiguration.
    """
    try:
        from db import connect as _db_connect
        return _db_connect()
    except Exception as e:
        logging.exception('Failed to obtain Postgres connection via db.connect(): %s', e)
        raise RuntimeError(f'Postgres connection required: {e}') from e
VALUE_JSON_DIR = os.path.join('data_output', 'value')

# 0 is Monday and 6 is Sunday. Change this value to change the weekly boundary.
WEEK_START_WEEKDAY = 0
# WEEK_START_WEEKDAY = 6  # Use this instead when weeks should start on Sunday.


def _sanitize_user_id(user_id: str) -> str:
    """Make a filesystem-safe directory name from user_id."""
    if not user_id:
        return 'unknown'
    # replace non-alphanumeric characters with underscore
    import re
    s = re.sub(r'[^0-9A-Za-z]+', '_', user_id)
    return s

def ensure_schema(db_path: str = DB_PATH):
    """Create tables if they do not exist."""
    # Schema management should be done via the Postgres migration/schema SQL.
    # If a SQL schema file exists under docker/init/create_schema_postgres.sql, try to apply it.
    try:
        conn = _get_conn(db_path)
        cur = conn.cursor()
        schema_path = os.path.join('docker', 'init', 'create_schema_postgres.sql')
        if os.path.exists(schema_path):
            with open(schema_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # naive split on semicolon for multiple statements
            for stmt in content.split(';'):
                stmt = stmt.strip()
                if not stmt:
                    continue
                try:
                    cur.execute(stmt)
                except Exception:
                    # ignore individual statement failures; operators should manage schema separately
                    logging.debug('ensure_schema: statement failed, continuing')
            try:
                conn.commit()
            except Exception:
                pass
        else:
            logging.info('ensure_schema: no local Postgres schema file found; skipping')
    finally:
        try:
            conn.close()
        except Exception:
            pass


from contextlib import contextmanager


@contextmanager
def user_lock(user_id: str, timeout_sec: int = 30):
    """Context manager to acquire a per-user lock.
    Uses portalocker if available (file lock). Otherwise falls back to a DB-based lock table.
    """
    safe = _sanitize_user_id(user_id)
    lock_dir = LOCKS_DIR
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f'{safe}.lock')
    # Use portalocker if available. If portalocker fails to lock (e.g. same-process deadlock
    # on Windows), fall back to an in-process threading.Lock to serialize threads in this
    # process while still allowing other processes to use file locks.
    if portalocker:
        f = open(lock_path, 'a+')
        try:
            start = time.time()
            acquired = False
            # Try non-blocking lock with short sleep until timeout
            while time.time() - start < timeout_sec:
                try:
                    portalocker.lock(f, portalocker.LOCK_EX | portalocker.LOCK_NB)
                    acquired = True
                    break
                except Exception:
                    time.sleep(0.05)

            if acquired:
                try:
                    yield
                finally:
                    try:
                        portalocker.unlock(f)
                    except Exception:
                        pass
                    try:
                        f.close()
                    except Exception:
                        pass
                return

            # If portalocker couldn't be acquired within timeout, fall back to in-process lock
            try:
                with _inproc_locks_lock:
                    lock = _inproc_locks.get(safe)
                    if lock is None:
                        lock = threading.Lock()
                        _inproc_locks[safe] = lock
                acquired2 = lock.acquire(timeout=timeout_sec)
                if not acquired2:
                    raise RuntimeError(f'Could not acquire in-process lock for user {user_id}')
                try:
                    yield
                finally:
                    try:
                        lock.release()
                    except Exception:
                        pass
            finally:
                try:
                    f.close()
                except Exception:
                    pass
        finally:
            try:
                f.close()
            except Exception:
                pass
        return

    # portalocker not available: use in-process lock only (avoid DB-based fallback to prevent DB contention)
    with _inproc_locks_lock:
        lock = _inproc_locks.get(safe)
        if lock is None:
            lock = threading.Lock()
            _inproc_locks[safe] = lock
    acquired = lock.acquire(timeout=timeout_sec)
    if not acquired:
        raise RuntimeError(f'Could not acquire in-process lock for user {user_id}')
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _execute_with_retry(conn, sql, params=(), retries=3, backoff=0.1):
    """Execute a write SQL with simple retry on 'database is locked'."""
    for attempt in range(retries):
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ('locked', 'busy', 'could not obtain lock', 'deadlock', 'timeout')):
                time.sleep(backoff * (2 ** attempt))
                continue
            raise
    # final attempt without catching
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def _persist_notification_id_for_row(row_id: int, notification_id: str, db_path: str = DB_PATH, retries: int = 4, backoff: float = 0.1) -> bool:
    """Attempt to persist `notification_id` (UUID/text) into `notifications.notification_id` for given row id.
    Retries transient DB lock errors with exponential backoff. Returns True on success.
    """
    if not row_id or not notification_id:
        return False
    for attempt in range(retries):
        conn = None
        try:
            conn = _get_conn(db_path)
            cur = conn.cursor()
            try:
                # Only set notification_id if it is currently NULL/empty to avoid
                # clobbering a mapping written by enqueue side or another process.
                try:
                    cur.execute('UPDATE notifications SET notification_id=%s WHERE id=%s AND (notification_id IS NULL OR notification_id = \'\')', (notification_id, row_id))
                except Exception:
                    # Fallback: some older schemas/drivers may not like the conditional
                    # expression; fall back to unconditional update.
                    cur.execute('UPDATE notifications SET notification_id=%s WHERE id=%s', (notification_id, row_id))
                try:
                    conn.commit()
                except Exception:
                    pass
                logging.info('storage_sqlite: persisted notification_id %s -> notifications.id %s', notification_id, row_id)
                return True
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in ('locked', 'busy', 'could not obtain lock', 'deadlock', 'timeout')):
                    time.sleep(backoff * (2 ** attempt))
                    continue
                # If schema lacks column, give up
                if 'no such column' in msg or 'column "notification_id"' in msg:
                    logging.warning('storage_sqlite: notifications.notification_id column missing; cannot persist mapping')
                    return False
                raise
        except Exception:
            try:
                logging.exception('storage_sqlite: error persisting notification_id (attempt %s) for notifications.id %s', attempt + 1, row_id)
            except Exception:
                pass
            time.sleep(backoff * (2 ** attempt))
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
    try:
        logging.error('storage_sqlite: failed to persist notification_id %s -> notifications.id %s after %s attempts', notification_id, row_id, retries)
    except Exception:
        pass
    return False


def _compute_notification_key(text: str = None, payload: str = None) -> str:
    """Compute a stable key for a notification from payload or text."""
    import hashlib
    src = None
    if payload:
        src = payload
    else:
        src = text or ''
    h = hashlib.sha256()
    h.update(src.encode('utf-8'))
    return h.hexdigest()


def allow_enqueue_for_user(user_id: str, window_seconds: int = 2 * 60 * 60, db_path: str = DB_PATH) -> bool:
    """Atomically check and update per-user last decision time.

    Returns True if enqueue is allowed (and updates last_decision_at to now),
    False if suppressed because the last decision was within window_seconds.
    """
    now = int(time.time())
    cutoff = now - int(window_seconds)
    conn = _get_conn(db_path)
    try:
        import db as _db
        logging.info('storage_sqlite: db.DB_URL=%s', getattr(_db, 'DB_URL', None))
    except Exception:
        logging.info('storage_sqlite: db module not available')
    logging.info('storage_sqlite: conn type=%s, is_pg=%s', type(conn), hasattr(conn, '_conn'))
    try:
        cur = conn.cursor()
        # ensure table exists
        cur.execute('CREATE TABLE IF NOT EXISTS user_notification_state(user_id TEXT PRIMARY KEY, last_decision_at BIGINT)')
        # Start transaction and lock the user's row
        cur.execute('BEGIN')
        cur.execute('SELECT last_decision_at FROM user_notification_state WHERE user_id=%s FOR UPDATE', (user_id,))
        row = cur.fetchone()
        last = row[0] if row and row[0] is not None else None
        if last is None or int(last) < cutoff:
            # upsert
            cur.execute('INSERT INTO user_notification_state(user_id, last_decision_at) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET last_decision_at = EXCLUDED.last_decision_at', (user_id, now))
            conn.commit()
            return True
        else:
            conn.rollback()
            return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _day_file_path(user_id: str, dt: datetime) -> str:
    tag = dt.strftime('%Y-%m-%d')
    safe_user = _sanitize_user_id(user_id)
    # New layout: data_output/<sanitized_user>/value/heart_YYYY-MM-DD.json
    user_base = os.path.join('data_output', safe_user)
    fname = f'heart_{tag}.json'
    return os.path.join(user_base, 'value', fname)


# def append_sample_json(user_id: str, dt: datetime, sample: dict) -> None:
#     """Append a per-minute sample to the user's day JSON file safely.
#     Uses portalocker if available for cross-process locking. Falls back to atomic replace.
#     """
#     path = _day_file_path(user_id, dt)
#     # ensure the per-user directory exists
#     user_dir = os.path.dirname(path)
#     os.makedirs(user_dir, exist_ok=True)
#     data = {'PerMinute': []}
#     # Acquire file lock if portalocker is present
#     if portalocker:
#         # open the file for read/write create if not exists
#         with open(path, 'a+', encoding='utf-8') as f:
#             try:
#                 portalocker.lock(f, portalocker.LOCK_EX)
#             except Exception:
#                 # if lock fails, fallback to simple atomic write below
#                 pass
#             try:
#                 f.seek(0)
#                 content = f.read()
#                 if content:
#                     try:
#                         existing = json.loads(content)
#                         data = existing
#                     except Exception:
#                         data = {'PerMinute': []}
#                 permin = data.get('PerMinute') or []
#                 entry = {
#                     # normalize to minute precision to avoid multiple entries within same minute
#                     'time': dt.strftime('%H:%M:00'),
#                     'heart_rate': sample.get('heart_rate'),
#                     'steps': sample.get('steps')
#                 }
#                 permin.append(entry)
#                 data['PerMinute'] = permin
#                 f.seek(0)
#                 f.truncate()
#                 json.dump(data, f, ensure_ascii=False, indent=2)
#                 f.flush()
#             finally:
#                 try:
#                     portalocker.unlock(f)
#                 except Exception:
#                     pass
#             return

#         # fallback: read-modify-write with atomic replace
#         if os.path.exists(path):
#             try:
#                 with open(path, 'r', encoding='utf-8') as f:
#                     content = f.read()
#                     if content:
#                         try:
#                             data = json.loads(content)
#                         except Exception:
#                             data = {'PerMinute': []}
#                     else:
#                         data = {'PerMinute': []}
#             except Exception:
#                 data = {'PerMinute': []}
#         else:
#             data = {'PerMinute': []}

#         permin = data.get('PerMinute') or []
#         entry = {
#             'time': dt.strftime('%H:%M:00'),
#             'heart_rate': sample.get('heart_rate'),
#             'steps': sample.get('steps')
#         }
#         permin.append(entry)
#         data['PerMinute'] = permin

#         # atomic write
#         import tempfile
#         dirpath = os.path.dirname(path)
#         fd, tmp = tempfile.mkstemp(prefix='tmp-heart-', dir=dirpath, text=True)
#         try:
#             with os.fdopen(fd, 'w', encoding='utf-8') as tf:
#                 json.dump(data, tf, ensure_ascii=False, indent=2)
#             os.replace(tmp, path)
#         except Exception:
#             try:
#                 if os.path.exists(tmp):
#                     os.remove(tmp)
#             except Exception:
#                 pass


    # --- Application-facing DB helpers ----------------------------------------

def _is_pg_conn(conn) -> bool:
    # PG wrapper in db.connect() exposes `_conn` attribute
    return hasattr(conn, '_conn')


def create_sent_notification(user_id: str, exercise_name: str, exercise_type: str, message_text: str, reps: int = None, sets: int = None, duration_min: int = None, ai_category: str = None, ai_rationale: str = None, detection_type: str = None, notification_id: str = None, ai_context: dict = None, db_path: str = DB_PATH) -> str:
    """Insert a sent_exercise_notifications row. Returns notification_id."""
    import uuid
    nid = notification_id or str(uuid.uuid4())
    con = _get_conn(db_path)
    try:
        cur = con.cursor()
        import json as _json
        try:
            # try inserting with ai_context column
            # Use a safe JSON serializer that converts datetimes (and similar objects)
            # to ISO strings to avoid TypeError: Object of type datetime is not JSON serializable
            def _json_default(o):
                try:
                    if hasattr(o, 'isoformat'):
                        return o.isoformat()
                except Exception:
                    pass
                try:
                    return str(o)
                except Exception:
                    return None

            def _make_jsonable(obj):
                # Recursively convert datetimes and other non-serializable types to strings
                if obj is None:
                    return None
                if isinstance(obj, (str, int, float, bool)):
                    return obj
                try:
                    from datetime import datetime, date
                    if isinstance(obj, (datetime, date)):
                        return obj.isoformat()
                except Exception:
                    pass
                if isinstance(obj, dict):
                    return {k: _make_jsonable(v) for k, v in obj.items()}
                if isinstance(obj, (list, tuple)):
                    return [_make_jsonable(v) for v in obj]
                # fallback to default handler
                try:
                    return _json_default(obj)
                except Exception:
                    try:
                        return str(obj)
                    except Exception:
                        return None

            try:
                ai_ctx_json = _json.dumps(ai_context, ensure_ascii=False, default=_json_default) if ai_context is not None else None
            except Exception:
                # fallback: recursively make structure JSON-safe then dump
                try:
                    safe_ctx = _make_jsonable(ai_context)
                    ai_ctx_json = _json.dumps(safe_ctx, ensure_ascii=False)
                except Exception:
                    ai_ctx_json = None
            cur.execute(
                'INSERT INTO sent_exercise_notifications (notification_id, user_id, exercise_name, exercise_type, sent_at, reps, sets, duration_min, ai_category, ai_rationale, detection_type, ai_context, message_text) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s)',
                (nid, user_id, exercise_name, exercise_type, reps, sets, duration_min, ai_category, ai_rationale, detection_type, ai_ctx_json, message_text),
            )
        except Exception as exc:
            # fallback for older schema without ai_context column
            msg = str(exc).lower()
            if 'no such column' not in msg and 'column "ai_context"' not in msg:
                raise
            cur.execute(
                'INSERT INTO sent_exercise_notifications (notification_id, user_id, exercise_name, exercise_type, sent_at, reps, sets, duration_min, message_text) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s)',
                (nid, user_id, exercise_name, exercise_type, reps, sets, duration_min, message_text),
            )
        con.commit()
    finally:
        try:
            con.close()
        except Exception:
            pass
    return nid


def update_sent_notification_ai_fields(
    notification_id: str,
    exercise_type: str = None,
    ai_category: str = None,
    ai_rationale: str = None,
    reps: int = None,
    sets: int = None,
    duration_min: int = None,
    message_text: str = None,
    db_path: str = DB_PATH,
) -> bool:
    """Overwrite AI-derived fields on an existing sent_exercise_notifications row.

    Used when the row was created before the final AI payload was known (e.g. the
    no_movement flow creates the row prior to the pre-survey answer), so the
    provisional values can be replaced once the real exercise message is decided.
    Only columns with a non-None argument are updated.
    """
    fields = []
    params = []
    if exercise_type is not None:
        fields.append('exercise_type = %s')
        params.append(exercise_type)
    if ai_category is not None:
        fields.append('ai_category = %s')
        params.append(ai_category)
    if ai_rationale is not None:
        fields.append('ai_rationale = %s')
        params.append(ai_rationale)
    if reps is not None:
        fields.append('reps = %s')
        params.append(reps)
    if sets is not None:
        fields.append('sets = %s')
        params.append(sets)
    if duration_min is not None:
        fields.append('duration_min = %s')
        params.append(duration_min)
    if message_text is not None:
        fields.append('message_text = %s')
        params.append(message_text)
    if not fields:
        return False

    fields.append('updated_at = NOW()')
    params.append(notification_id)
    con = _get_conn(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            f"UPDATE sent_exercise_notifications SET {', '.join(fields)} WHERE notification_id = %s",
            tuple(params),
        )
        rowcount = cur.rowcount
        try:
            con.commit()
        except Exception:
            pass
        return bool(rowcount)
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_sent_notification(notification_id: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieve a sent_exercise_notifications row as a dict (includes parsed ai_context if present)."""
    con = _get_conn(db_path)
    try:
        cur = con.cursor()
        try:
            cur.execute('SELECT notification_id, user_id, exercise_name, exercise_type, sent_at, ai_category, ai_rationale, detection_type, ai_context, message_text FROM sent_exercise_notifications WHERE notification_id = %s', (notification_id,))
        except Exception:
            # older schema without ai_context
            cur.execute('SELECT notification_id, user_id, exercise_name, exercise_type, sent_at, ai_category, ai_rationale, detection_type, message_text FROM sent_exercise_notifications WHERE notification_id = %s', (notification_id,))
            row = cur.fetchone()
            if not row:
                return None
            keys = ['notification_id', 'user_id', 'exercise_name', 'exercise_type', 'sent_at', 'ai_category', 'ai_rationale', 'detection_type', 'message_text']
            return dict(zip(keys, row))
        row = cur.fetchone()
        if not row:
            return None
        keys = ['notification_id', 'user_id', 'exercise_name', 'exercise_type', 'sent_at', 'ai_category', 'ai_rationale', 'detection_type', 'ai_context', 'message_text']
        res = dict(zip(keys, row))
        # parse ai_context JSON if present
        try:
            import json as _json
            if res.get('ai_context'):
                res['ai_context'] = _json.loads(res['ai_context'])
            else:
                res['ai_context'] = None
        except Exception:
            res['ai_context'] = None
        return res
    finally:
        try:
            con.close()
        except Exception:
            pass


def _get_survey_session_record(lookup_column: str, lookup_value: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Return the persisted data needed to restore one Slack survey session."""
    if lookup_column not in ('message_ts', 'notification_id') or not lookup_value:
        return None

    con = _get_conn(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            '''
            SELECT
                n.notification_id,
                n.user_id,
                n.channel,
                s.exercise_type,
                s.message_text,
                s.ai_context,
                s.detection_type
            FROM notifications AS n
            LEFT JOIN sent_exercise_notifications AS s
                ON s.notification_id = n.notification_id
            WHERE n.{} = %s
            ORDER BY n.id DESC
            LIMIT 1
            '''.format(lookup_column),
            (lookup_value,),
        )
        row = cur.fetchone()
        if not row:
            return None

        keys = [
            'notification_id',
            'user_id',
            'channel',
            'exercise_type',
            'message_text',
            'ai_context',
            'detection_type',
        ]
        result = dict(zip(keys, row))
        try:
            ai_context = result.get('ai_context')
            result['ai_context'] = json.loads(ai_context) if ai_context else {}
        except Exception:
            result['ai_context'] = {}
        return result
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_survey_session_by_message_ts(message_ts: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Return persisted Slack survey data using the Slack message timestamp."""
    return _get_survey_session_record('message_ts', message_ts, db_path)


def get_survey_session_by_notification_id(notification_id: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Return persisted Slack survey data using the notification identifier."""
    return _get_survey_session_record('notification_id', notification_id, db_path)


def create_pending_response(notification_id: str, user_id: str, db_path: str = DB_PATH) -> None:
    """Insert a pending received_exercise_responses row for given notification_id."""
    # Before inserting a new pending response, mark any existing pending responses
    # for this user as superseded (not_implemented) to avoid unbounded growth
    # and to reflect that a new notification supersedes the old one.
    # Use per-user lock to avoid races with concurrent sends/updates.
    with user_lock(user_id):
        con = _get_conn(db_path)
        try:
            cur = con.cursor()
            try:
                # mark older pending responses as not_implemented (superseded)
                cur.execute(
                    """
                    UPDATE received_exercise_responses
                    SET response_status='not_implemented', implemented_flag=0, not_implemented_reason='superseded_by_new_notification', updated_at = NOW()
                    WHERE user_id = %s AND response_status = 'pending'
                    """,
                    (user_id,),
                )
            except Exception:
                # If alter/update fails due to schema differences, ignore and continue
                pass

            # insert the new pending response
            cur.execute(
                "INSERT INTO received_exercise_responses (notification_id, user_id, response_status) VALUES (%s, %s, 'pending')",
                (notification_id, user_id),
            )
            con.commit()
        finally:
            try:
                con.close()
            except Exception:
                pass


def update_response(notification_id: str, response_status: str, implemented_flag: int = None, not_implemented_reason: str = None, visibility_before: int = None, perceived_exertion: int = None, db_path: str = DB_PATH) -> None:
    """Update an existing received_exercise_responses row identified by notification_id."""
    if response_status not in ('pending', 'implemented', 'not_implemented'):
        raise ValueError('invalid response_status')
    con = _get_conn(db_path)
    try:
        cur = con.cursor()
        fields = []
        params = []
        fields.append('response_status = %s')
        params.append(response_status)
        if implemented_flag is not None:
            fields.append('implemented_flag = %s')
            params.append(implemented_flag)
        if not_implemented_reason is not None:
            fields.append('not_implemented_reason = %s')
            params.append(not_implemented_reason)
        if visibility_before is not None:
            fields.append('visibility_before = %s')
            # Some DB schemas store visibility_before as text; cast to str to avoid
            # Postgres operator type mismatch (text = integer) when binding params.
            try:
                params.append(str(visibility_before))
            except Exception:
                params.append(visibility_before)
        if perceived_exertion is not None:
            fields.append('perceived_exertion = %s')
            params.append(perceived_exertion)
        if not fields:
            return

        # Prepare notification id parameter; prefer stored notification_id (UUID/text)
        try:
            nid_param = str(notification_id)
        except Exception:
            nid_param = notification_id

        # If nid_param looks numeric, try resolving it via notifications.id -> notifications.notification_id
        try:
            if nid_param and isinstance(nid_param, str) and nid_param.isdigit():
                connr = None
                try:
                    connr = _get_conn(db_path)
                    cur_r = connr.cursor()
                    cur_r.execute('SELECT notification_id FROM notifications WHERE id = %s LIMIT 1', (int(nid_param),))
                    rr = cur_r.fetchone()
                    if rr and rr[0]:
                        try:
                            logging.info('storage_sqlite: resolved numeric notifications.id %s -> notification_id=%s', nid_param, rr[0])
                        except Exception:
                            pass
                        nid_param = rr[0]
                except Exception:
                    try:
                        logging.exception('storage_sqlite: failed resolving numeric notification id %s', nid_param)
                    except Exception:
                        pass
                finally:
                    try:
                        if connr:
                            connr.close()
                    except Exception:
                        pass
        except Exception:
            pass

        # Ensure notification_id is passed as text to match DB schema (notification_id TEXT)
        params.append(nid_param)

        # Force text comparison to avoid operator errors when notification_id
        # values may be integers in some code paths; cast both sides to text.
        sql = f"UPDATE received_exercise_responses SET {', '.join(fields)}, updated_at = NOW() WHERE notification_id::text = %s::text"
        cur.execute(sql, tuple(params))
        rowcount = cur.rowcount

        # If no rows updated, fallback: maybe caller passed numeric notifications.id or DB uses id column
        if rowcount == 0:
            try:
                nid_int = int(nid_param)
            except Exception:
                nid_int = None
            if nid_int is not None:
                try:
                    sql2 = f"UPDATE received_exercise_responses SET {', '.join(fields)}, updated_at = NOW() WHERE id = %s"
                    cur.execute(sql2, tuple(params[:-1] + [nid_int]))
                    rowcount = cur.rowcount
                except Exception:
                    pass

        # commit and return number of updated rows for caller to inspect
        try:
            con.commit()
        except Exception:
            pass
        return rowcount
    finally:
        try:
            con.close()
        except Exception:
            pass


def resolve_notification_id(notification_id: str, db_path: str = DB_PATH) -> Optional[str]:
    """Resolve a notification identifier to the textual UUID used by response tables.

    If a numeric notifications.id is provided, this returns notifications.notification_id
    when available. Otherwise returns the input value as-is.
    """
    if notification_id is None:
        return None
    try:
        nid_param = str(notification_id)
    except Exception:
        nid_param = notification_id
    try:
        if nid_param and isinstance(nid_param, str) and nid_param.isdigit():
            conn = _get_conn(db_path)
            try:
                cur = conn.cursor()
                cur.execute('SELECT notification_id FROM notifications WHERE id = %s LIMIT 1', (int(nid_param),))
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception:
        pass
    return nid_param


def get_response_by_notification_id(notification_id: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Fetch one received_exercise_responses row by notification_id (or notifications.id)."""
    nid = resolve_notification_id(notification_id, db_path=db_path)
    if not nid:
        return None
    conn = _get_conn(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT id, notification_id, user_id, response_status, implemented_flag,
                   not_implemented_reason, visibility_before, perceived_exertion, updated_at
            FROM received_exercise_responses
            WHERE notification_id::text = %s::text
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            ''',
            (nid,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def upsert_response_by_notification_id(
    notification_id: str,
    response_status: str,
    implemented_flag: int = None,
    not_implemented_reason: str = None,
    visibility_before: int = None,
    perceived_exertion: int = None,
    user_id: str = None,
    db_path: str = DB_PATH,
) -> int:
    """Update response by notification_id; create missing pending row and retry if needed.

    This keeps response updates anchored to the same notification_id and avoids
    creating a new notification record in interactive answer flows.
    """
    nid = resolve_notification_id(notification_id, db_path=db_path)
    if not nid:
        return 0

    rows = update_response(
        notification_id=nid,
        response_status=response_status,
        implemented_flag=implemented_flag,
        not_implemented_reason=not_implemented_reason,
        visibility_before=visibility_before,
        perceived_exertion=perceived_exertion,
        db_path=db_path,
    )
    if rows:
        return rows

    uid = user_id
    if not uid:
        conn = _get_conn(db_path)
        try:
            cur = conn.cursor()
            # Prefer sent_exercise_notifications owner because it is canonical for this UUID.
            cur.execute('SELECT user_id FROM sent_exercise_notifications WHERE notification_id = %s LIMIT 1', (nid,))
            row = cur.fetchone()
            if row and row[0]:
                uid = row[0]
            else:
                cur.execute('SELECT user_id FROM notifications WHERE notification_id = %s LIMIT 1', (nid,))
                row2 = cur.fetchone()
                if row2 and row2[0]:
                    uid = row2[0]
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if not uid:
        return 0

    try:
        create_pending_response(nid, uid, db_path=db_path)
    except Exception:
        # If row already exists or insert failed transiently, continue to retry update.
        pass

    return update_response(
        notification_id=nid,
        response_status=response_status,
        implemented_flag=implemented_flag,
        not_implemented_reason=not_implemented_reason,
        visibility_before=visibility_before,
        perceived_exertion=perceived_exertion,
        db_path=db_path,
    ) or 0


def get_recent_exercise_history(user_id: str, limit: int = 5, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Return recent sent notifications joined with their latest responses.

    The result is ordered from newest to oldest and intentionally excludes the
    original message_text so prompt payloads stay compact.
    """
    con = _get_conn(db_path)
    try:
        cur = con.cursor()
        try:
            cur.execute(
                '''
                    SELECT
                        s.notification_id,
                        s.exercise_name,
                        s.exercise_type,
                        s.sent_at,
                        s.reps,
                        s.sets,
                        s.duration_min,
                        s.ai_category,
                        s.ai_rationale,
                        r.response_status,
                        r.implemented_flag,
                        r.not_implemented_reason,
                        r.visibility_before,
                        r.perceived_exertion,
                        r.updated_at AS response_updated_at
                    FROM sent_exercise_notifications AS s
                    LEFT JOIN received_exercise_responses AS r
                        ON r.notification_id = s.notification_id
                    WHERE s.user_id = %s
                    ORDER BY s.sent_at DESC, s.id DESC
                    LIMIT %s
                ''',
                (user_id, limit),
            )
        except Exception as exc:
            # fallback for older schema
            if 'no such column' not in str(exc).lower():
                raise
            cur.execute(
                '''
                    SELECT
                        s.notification_id,
                        s.exercise_name,
                        s.exercise_type,
                        s.sent_at,
                        s.reps,
                        s.sets,
                        s.duration_min,
                        NULL AS ai_category,
                        NULL AS ai_rationale,
                        r.response_status,
                        r.implemented_flag,
                        r.not_implemented_reason,
                        r.visibility_before,
                        r.perceived_exertion,
                        r.updated_at AS response_updated_at
                    FROM sent_exercise_notifications AS s
                    LEFT JOIN received_exercise_responses AS r
                        ON r.notification_id = s.notification_id
                    WHERE s.user_id = %s
                    ORDER BY s.sent_at DESC, s.id DESC
                    LIMIT %s
                ''',
                (user_id, limit),
            )
        columns = [desc[0] for desc in cur.description]
        rows: List[Dict[str, Any]] = []
        for row in cur.fetchall():
            rows.append(dict(zip(columns, row)))
        return rows
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_latest_sent_notification(user_id: str, within_minutes: int = 15, db_path: str = DB_PATH) -> Optional[str]:
    """Return the notification_id of the most recent sent_exercise_notifications for user_id
    if sent within `within_minutes`. Returns None if not found.
    """
    conn = _get_conn(db_path)
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT notification_id, sent_at FROM sent_exercise_notifications WHERE user_id = %s ORDER BY sent_at DESC LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
        except Exception:
            return None
        if not row:
            return None
        nid, sent_at = row[0], row[1]
        try:
            from datetime import datetime, timezone
            if isinstance(sent_at, datetime):
                dt = sent_at
            else:
                dt = datetime.fromisoformat(str(sent_at))
            # ensure timezone-aware
            if dt.tzinfo is None:
                try:
                    dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                except Exception:
                    pass
            from datetime import timedelta
            if datetime.now(dt.tzinfo) - dt <= timedelta(minutes=within_minutes):
                return nid
        except Exception:
            return nid
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_exercise_adherence_by_type(user_id: str, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Return adherence summary grouped by exercise_type for one user."""
    con = _get_conn(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            '''
                SELECT
                    s.exercise_type AS exercise_type,
                    COUNT(*) AS presented_count,
                    COALESCE(SUM(CASE WHEN r.response_status = 'implemented' AND r.implemented_flag = 1 THEN 1 ELSE 0 END), 0) AS implemented_count,
                    CASE
                        WHEN COUNT(*) = 0 THEN 0.0
                        ELSE ROUND(100.0 * COALESCE(SUM(CASE WHEN r.response_status = 'implemented' AND r.implemented_flag = 1 THEN 1 ELSE 0 END), 0) / COUNT(*), 1)
                    END AS adherence_rate
                FROM sent_exercise_notifications AS s
                LEFT JOIN received_exercise_responses AS r
                    ON r.notification_id = s.notification_id
                WHERE s.user_id = ?
                GROUP BY s.exercise_type
                ORDER BY s.exercise_type
            ''',
            (user_id,),
        )
        columns = [desc[0] for desc in cur.description]
        rows: List[Dict[str, Any]] = []
        for row in cur.fetchall():
            rows.append(dict(zip(columns, row)))
        return rows
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_weekly_implemented_exercise_counts(
    user_id: str, now: Optional[datetime] = None, db_path: str = DB_PATH
) -> Dict[str, Any]:
    """Return this week's implemented exercise counts grouped by exercise type.

    The reporting week is determined from sent_exercise_notifications.sent_at,
    so an exercise belongs to the week when it was presented to the user.
    """
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.now().astimezone().tzinfo)
    days_since_week_start = (now.weekday() - WEEK_START_WEEKDAY) % 7
    period_start = (now - timedelta(days=days_since_week_start)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    period_end = period_start + timedelta(days=7)

    counts = {"cardio": 0, "strength": 0, "stretch": 0}
    con = _get_conn(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            """
                SELECT s.exercise_type, COUNT(*) AS implemented_count
                FROM sent_exercise_notifications AS s
                INNER JOIN received_exercise_responses AS r
                    ON r.notification_id = s.notification_id
                WHERE s.user_id = %s
                  AND s.sent_at >= %s
                  AND s.sent_at < %s
                  AND r.response_status = 'implemented'
                  AND r.implemented_flag = 1
                GROUP BY s.exercise_type
                ORDER BY s.exercise_type
            """,
            (user_id, period_start, period_end),
        )
        for exercise_type, implemented_count in cur.fetchall():
            if exercise_type:
                counts[str(exercise_type)] = int(implemented_count or 0)
    finally:
        try:
            con.close()
        except Exception:
            pass

    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "week_start_weekday": WEEK_START_WEEKDAY,
        "implemented_counts_by_type": [
            {"exercise_type": exercise_type, "implemented_count": count}
            for exercise_type, count in sorted(counts.items())
        ],
    }


def get_recent_exercise_context(user_id: str, limit: int = 5, db_path: str = DB_PATH) -> Dict[str, Any]:
    """Build a compact context payload for AI prompt generation.

    This keeps message_text out of the payload and returns only fields that are
    directly useful for exercise selection, justification, and personalization.
    """
    return {
        'recent_exercises': get_recent_exercise_history(user_id, limit=limit, db_path=db_path),
        'adherence_by_type': get_exercise_adherence_by_type(user_id, db_path=db_path),
        'weekly_implemented_exercise_counts': get_weekly_implemented_exercise_counts(user_id, db_path=db_path),
    }


def build_exercise_ai_input(
    user_id: str,
    exercise_type: str,
    exercise_name: Optional[str] = None,
    event: Optional[Dict[str, Any]] = None,
    pre_survey: Optional[Dict[str, Any]] = None,
    limit: int = 5,
    db_path: str = DB_PATH,
) -> Dict[str, Any]:
    """Build the compact AI input payload for exercise message generation.

    The payload intentionally excludes raw physiological samples and message_text.
    Only high-level metadata and DB-derived history are included.
    """
    event = event or {}
    recent_context = get_recent_exercise_context(user_id, limit=limit, db_path=db_path)
    recent_exercises = []
    for row in recent_context.get('recent_exercises', []):
        recent_exercises.append({
            'exercise_name': row.get('exercise_name'),
            'exercise_type': row.get('exercise_type'),
            'sent_at': row.get('sent_at'),
            'reps': row.get('reps'),
            'sets': row.get('sets'),
            'duration_min': row.get('duration_min'),
            'ai_category': row.get('ai_category'),
            'ai_rationale': row.get('ai_rationale'),
            'response_status': row.get('response_status'),
            'implemented_flag': row.get('implemented_flag'),
            'not_implemented_reason': row.get('not_implemented_reason'),
            'visibility_before': row.get('visibility_before'),
            'perceived_exertion': row.get('perceived_exertion'),
        })

    adherence_by_type = []
    for row in recent_context.get('adherence_by_type', []):
        adherence_by_type.append({
            'exercise_type': row.get('exercise_type'),
            'presented_count': row.get('presented_count'),
            'implemented_count': row.get('implemented_count'),
            'adherence_rate': row.get('adherence_rate'),
        })

    weekly_implemented_exercise_counts = recent_context.get(
        'weekly_implemented_exercise_counts',
        {'implemented_counts_by_type': []},
    )

    pre_visibility = None
    if isinstance(pre_survey, dict):
        pre_visibility = pre_survey.get('visibility')

    detected_at = None
    if isinstance(event, dict):
        detected_at = event.get('ts')
        if hasattr(detected_at, 'isoformat'):
            try:
                detected_at = detected_at.isoformat()
            except Exception:
                detected_at = str(detected_at)

    return {
        'exercise_name': exercise_name,
        'exercise_type': exercise_type,
        'motion_detected': event.get('type') != 'no_movement' if isinstance(event, dict) else None,
        'detected_event_type': event.get('type') if isinstance(event, dict) else None,
        'detected_at': detected_at,
        'pre_survey': {
            'visibility': pre_visibility,
        },
        'recent_exercises': recent_exercises,
        'adherence_by_type': adherence_by_type,
        'weekly_implemented_exercise_counts': weekly_implemented_exercise_counts,
    }


# realtime DB storage functions removed: samples table is deprecated in this deployment


def get_recent_samples(user_id: str, minutes: int, db_path: str = DB_PATH) -> List[Tuple[str, Optional[int], Optional[int]]]:
    """Read recent per-minute samples from per-user JSON files.

    Returns a list of tuples (iso_timestamp_str, heart_rate, steps) for samples
    whose timestamp is within the last `minutes` minutes. This reads today's
    and yesterday's day-files to handle short cross-midnight ranges.
    """
    from datetime import datetime, timedelta
    import json

    now = datetime.now().astimezone()
    cutoff = now - timedelta(minutes=minutes)
    results = []

    # consider today and yesterday to cover short lookbacks across midnight
    dates = [now.date(), (now - timedelta(days=1)).date()]
    for d in dates:
        # build path using helper
        try:
            dt = datetime(d.year, d.month, d.day)
            path = _day_file_path(user_id, dt)
        except Exception:
            continue
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        permin = data.get('PerMinute') or []
        for e in permin:
            t = e.get('time')
            if not t:
                continue
            try:
                # time is expected as HH:MM:SS
                hhmmss = t
                tm = datetime.strptime(hhmmss, '%H:%M:%S').time()
                ts = datetime(d.year, d.month, d.day, tm.hour, tm.minute, tm.second, tzinfo=now.tzinfo)
            except Exception:
                # try parsing full ISO ts if present
                try:
                    ts = datetime.fromisoformat(t)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=now.tzinfo)
                except Exception:
                    continue
            if ts < cutoff:
                continue
            hr = e.get('heart_rate')
            steps = e.get('steps')
            results.append((ts.isoformat(), hr, steps))

    # sort by timestamp ascending
    results.sort(key=lambda r: r[0])
    return results


def enqueue_notification(user_id: str, channel: str, text: str, payload: str = None, notification_id: str = None, db_path: str = DB_PATH, skip_allow_check: bool = False) -> int:
    # Compute notification_key and optionally check per-user rate window before inserting
    key = _compute_notification_key(text=text, payload=payload)
    if not skip_allow_check:
        allowed = allow_enqueue_for_user(user_id)
        if not allowed:
            return None

    conn = _get_conn(db_path)
    try:
        # Short-window dedupe: skip same content for same user when a near-identical
        # notification was just created/sent very recently.
        try:
            cur_d = conn.cursor()
            cur_d.execute(
                """
                SELECT id
                FROM notifications
                WHERE user_id=%s
                  AND notification_key=%s
                  AND status IN ('pending','reserved','processing','sent')
                  AND created_at >= (NOW() - INTERVAL '120 seconds')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, key),
            )
            dupe = cur_d.fetchone()
            if dupe:
                try:
                    logging.info('enqueue_notification: suppressed duplicate content for user=%s, existing_id=%s', user_id, dupe[0])
                except Exception:
                    pass
                return None
        except Exception:
            # If dedupe query fails, proceed with normal insert path.
            pass

        now = datetime.now().astimezone().isoformat()
        cur = conn.cursor()
        # include notification_key and (optionally) notification_id when schema supports it
        if notification_id:
                try:
                    cur.execute(
                        'INSERT INTO notifications(user_id, channel, text, payload, notification_key, notification_id, status, attempts, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                        (user_id, channel, text, payload, key, notification_id, 'pending', 0, now),
                    )
                    nid = cur.fetchone()[0]
                except Exception:
                    # Try older schema variants: without notification_key but with notification_id
                    try:
                        cur.execute(
                            'INSERT INTO notifications(user_id, channel, text, payload, notification_id, status, attempts, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                            (user_id, channel, text, payload, notification_id, 'pending', 0, now),
                        )
                        nid = cur.fetchone()[0]
                    except Exception:
                        # final fallback: insert without notification_id
                        cur.execute('INSERT INTO notifications(user_id, channel, text, payload, status, attempts, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id',
                                    (user_id, channel, text, payload, 'pending', 0, now))
                        nid = cur.fetchone()[0]
        else:
                # no notification_id supplied: behave as before, prefer notification_key column
                try:
                    cur.execute('INSERT INTO notifications(user_id, channel, text, payload, notification_key, status, attempts, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                                (user_id, channel, text, payload, key, 'pending', 0, now))
                    nid = cur.fetchone()[0]
                except Exception:
                    # try older schema fallback
                    cur.execute('INSERT INTO notifications(user_id, channel, text, payload, status, attempts, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id',
                                (user_id, channel, text, payload, 'pending', 0, now))
                    nid = cur.fetchone()[0]
        try:
            conn.commit()
        except Exception:
            pass
        return nid
    finally:
        try:
            conn.close()
        except Exception:
            pass


def enqueue_notification_for_user(user_id: str, text: str, notification_id: str = None, payload: str = None, db_path: str = DB_PATH, create_if_missing: bool = False, create_pending: bool = False) -> int:
    """Resolve the Slack channel for `user_id` via `tokens_db.get_channel_for_user` and
    enqueue a notification. If the channel cannot be resolved, record a failed
    notification row (status='failed') so operators can inspect and retry.

    Returns the notifications table row id (int).
    """
    # Import tokens_db lazily to avoid circular imports in some callers
    try:
        import tokens_db
    except Exception:
        tokens_db = None

    try:
        channel = None
        last_exc = None
        if tokens_db:
            # Retry on transient errors (e.g. DB lock). Do not endlessly retry on missing mapping.
            for attempt in range(3):
                try:
                    channel = tokens_db.get_channel_for_user(user_id)
                    break
                except Exception as e:
                    last_exc = e
                    logging.warning('tokens_db.get_channel_for_user failed for %s (attempt %s): %s', user_id, attempt + 1, e)
                    time.sleep(0.2 * (2 ** attempt))
            # final quick re-check if no mapping found but no exception
            if channel is None and last_exc is None:
                try:
                    time.sleep(0.1)
                    channel = tokens_db.get_channel_for_user(user_id)
                except Exception as e:
                    last_exc = e

        # If no channel, record as failed so it's visible for reprocessing
        allowed = allow_enqueue_for_user(user_id)
        if not allowed:
            logging.info('Suppressed enqueue for user %s due to recent notification', user_id)
            return None

        if not channel:
            logging.warning('No channel mapping for user %s; inserting failed notification (last_exc=%s)', user_id, repr(last_exc))
            conn = _get_conn(db_path)
            try:
                now = datetime.now().astimezone().isoformat()
                reason = f'lookup_exception: {str(last_exc)}' if last_exc is not None else None
                cur = conn.cursor()
                if reason:
                    cur.execute(
                        'INSERT INTO notifications(user_id, channel, text, payload, status, attempts, created_at, last_attempt) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                        (user_id, None, (text or '') + '\nFailure reason: ' + reason, payload, 'failed', 0, now, now),
                    )
                else:
                    cur.execute(
                        'INSERT INTO notifications(user_id, channel, text, payload, status, attempts, created_at, last_attempt) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                        (user_id, None, text, payload, 'failed', 0, now, now),
                    )
                try:
                    nid = cur.fetchone()[0]
                except Exception:
                    nid = None
                try:
                    conn.commit()
                except Exception:
                    pass
                return nid
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        # If caller didn't supply a related sent_exercise_notifications.notification_id
        # and create_if_missing is requested, create the sent_exercise_notifications
        # record now so we can persist the mapping into notifications and pending responses.
        nid_to_use = None
        try:
            nid_to_use = str(notification_id) if notification_id is not None else None
        except Exception:
            nid_to_use = notification_id

        if nid_to_use is None and create_if_missing:
            try:
                nid_created = create_sent_notification(user_id, 'auto-generated', 'system', message_text=text)
                nid_to_use = nid_created
                logging.info('enqueue: create_if_missing created notification_id=%s for user=%s', nid_to_use, user_id)
                if create_pending:
                    try:
                        create_pending_response(nid_to_use, user_id)
                    except Exception:
                        logging.exception('create_pending_response failed for %s', nid_to_use)
            except Exception:
                logging.exception('create_sent_notification failed during create_if_missing for user %s', user_id)

        try:
            nid = enqueue_notification(user_id, channel, text, payload=payload, notification_id=nid_to_use, db_path=db_path, skip_allow_check=True)
            # If caller supplied a related sent_exercise_notifications.notification_id,
            # persist notification_id (either provided by caller or created above)
            try:
                nid_to_write = nid_to_use
                if nid_to_write:
                    ok = _persist_notification_id_for_row(nid, nid_to_write, db_path=db_path)
                    if not ok:
                        # Log detailed context for operator inspection
                        try:
                            logging.warning('enqueue_notification_for_user: failed to persist notification_id %s for notifications.id %s', nid_to_write, nid)
                        except Exception:
                            pass
            except Exception:
                # Non-fatal; proceed even if we couldn't write back mapping
                try:
                    logging.exception('enqueue_notification_for_user: unexpected error while persisting notification_id')
                except Exception:
                    pass
            # Immediately mark this enqueue as a reserved attempt so suppression logic
            # sees a recent last_attempt without waiting for the async worker.
            try:
                conn2 = _get_conn(db_path)
                try:
                    cur2 = conn2.cursor()
                    now = datetime.now().astimezone().isoformat()
                    cur2.execute('UPDATE notifications SET status=%s, last_attempt=%s WHERE id=%s', ('reserved', now, nid))
                    try:
                        conn2.commit()
                    except Exception:
                        pass
                finally:
                    try:
                        conn2.close()
                    except Exception:
                        pass
            except Exception:
                # ignore failures marking reserved
                pass
            return nid
        except Exception:
            # On unexpected enqueue failure, persist a failed row for visibility
            conn = _get_conn(db_path)
            try:
                now = datetime.now().astimezone().isoformat()
                cur = conn.cursor()
                if _is_pg_conn(conn):
                    cur.execute(
                        'INSERT INTO notifications(user_id, channel, text, payload, status, attempts, created_at, last_attempt) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                        (user_id, channel, text, payload, 'failed', 0, now, now),
                    )
                    try:
                        nid = cur.fetchone()[0]
                    except Exception:
                        nid = None
                else:
                    cur = _execute_with_retry(
                        conn,
                        'INSERT INTO notifications(user_id, channel, text, payload, status, attempts, created_at, last_attempt) VALUES(?,?,?,?,?,?,?,?)',
                        (user_id, channel, text, payload, 'failed', 0, now, now),
                    )
                    nid = cur.lastrowid
                try:
                    conn.commit()
                except Exception:
                    pass
                return nid
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception:
        # As a last resort, attempt to insert a failure record so the event is not lost
        try:
            conn = _get_conn(db_path)
            now = datetime.now().astimezone().isoformat()
            cur = conn.cursor()
            if _is_pg_conn(conn):
                cur.execute(
                    'INSERT INTO notifications(user_id, channel, text, status, attempts, created_at, last_attempt) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id',
                    (user_id, None, text, 'failed', 0, now, now),
                )
                try:
                    nid = cur.fetchone()[0]
                except Exception:
                    nid = None
            else:
                cur = _execute_with_retry(
                    conn,
                    'INSERT INTO notifications(user_id, channel, text, status, attempts, created_at, last_attempt) VALUES(?,?,?,?,?,?,?)',
                    (user_id, None, text, 'failed', 0, now, now),
                )
                nid = cur.lastrowid
            try:
                conn.commit()
            except Exception:
                pass
            return nid
        except Exception:
            # If even this fails, raise so caller is aware
            raise


def pop_pending_notifications(limit: int = 10, db_path: str = None) -> List[Tuple[int, str, str, str, str]]:
    """Fetch pending notifications and mark them as in-progress (status->processing) in a small transaction.
    Returns list of (id, user_id, channel, text).
    """
    if db_path is None:
        db_path = DB_PATH
    conn = _get_conn(db_path)
    try:
        cur = conn.cursor()
        try:
            # Require Postgres backend. This codebase now targets Postgres only.
            if not _is_pg_conn(conn):
                raise RuntimeError('Postgres connection required for pop_pending_notifications')

            cur.execute('BEGIN')
            cur.execute('SELECT id, user_id, channel, text, payload FROM notifications WHERE status IN (%s,%s) ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED', ('pending', 'reserved', limit))
            rows = cur.fetchall()
            ids = [r[0] for r in rows]
            if ids:
                placeholders = ','.join(['%s'] * len(ids))
                sql = f'UPDATE notifications SET status=%s WHERE id IN ({placeholders})'
                params = tuple(['processing'] + ids)
                cur.execute(sql, params)
            conn.commit()
            return rows
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def mark_notification_sent(row_id: int, message_ts: str = None, notification_id: str = None, db_path: str = DB_PATH) -> None:
    """Mark the given notifications.id as sent and record message_ts/sent_at and optional notification_id."""
    # Use _get_conn to ensure PRAGMAs are applied and retry on busy/locked
    conn = _get_conn(db_path)
    try:
        now = datetime.now().astimezone().isoformat()
        # If notification_id not provided, attempt best-effort resolution by matching
        # a sent_exercise_notifications row for the same user with similar text/time.
        resolved_nid = notification_id
        if resolved_nid is None:
            try:
                cur = conn.cursor()
                cur.execute('SELECT user_id, text, created_at FROM notifications WHERE id = %s LIMIT 1', (row_id,))
                row = cur.fetchone()
                if row:
                    user_id, text, created_at = row
                    try:
                        # look for recent sent_exercise_notifications for this user
                        cur.execute('SELECT notification_id, sent_at, message_text FROM sent_exercise_notifications WHERE user_id = %s ORDER BY sent_at DESC LIMIT 50', (user_id,))
                        cand = None
                        best_score = None
                        for s_nid, s_sent_at, s_text in cur.fetchall():
                            if s_text is None:
                                continue
                            score = 0
                            try:
                                if s_text == (text or ''):
                                    score = 100
                                elif text and (text in s_text):
                                    score = 80
                                elif s_text and (s_text in (text or '')):
                                    score = 60
                                else:
                                    score = 0
                            except Exception:
                                score = 0
                            if score == 0:
                                continue
                            try:
                                diff = abs((s_sent_at - created_at).total_seconds()) if s_sent_at and created_at else 999999
                            except Exception:
                                diff = 999999
                            if diff > 300:
                                continue
                            key = (score, -diff)
                            if best_score is None or key > best_score:
                                best_score = key
                                cand = s_nid
                        if cand:
                            resolved_nid = cand
                    except Exception:
                        resolved_nid = None
            except Exception:
                resolved_nid = None

        # Always mark the row as sent; write notification_id separately to avoid
        # blocking status transition when notification_id already exists.
        sql = 'UPDATE notifications SET status=%s, message_ts=%s, sent_at=%s, updated_at=%s WHERE id=%s'
        params = ('sent', message_ts, now, now, row_id)
        # Try with retries for transient DB locks
        for attempt in range(4):
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, params)
                except Exception:
                    # fallback for older schema without updated_at column
                    cur.execute('UPDATE notifications SET status=%s, message_ts=%s, sent_at=%s WHERE id=%s', ('sent', message_ts, now, row_id))

                # Write notification_id only when we have a resolved value and the column is empty.
                if resolved_nid is not None:
                    try:
                        cur.execute('UPDATE notifications SET notification_id=%s WHERE id=%s AND (notification_id IS NULL OR notification_id = %s)', (resolved_nid, row_id, ''))
                    except Exception:
                        # Best effort for older schema/driver differences.
                        try:
                            cur.execute('UPDATE notifications SET notification_id=%s WHERE id=%s', (resolved_nid, row_id))
                        except Exception:
                            pass
                conn.commit()
                break
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in ('locked', 'busy', 'could not obtain lock', 'deadlock', 'timeout')):
                    # exponential backoff
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def mark_notification_failed(row_id: int, reason: str = None, db_path: str = DB_PATH) -> None:
    """Mark the notification as failed and record last_attempt timestamp and possibly note the reason in text."""
    conn = _get_conn(db_path)
    try:
        cur = conn.cursor()
        now = datetime.now().astimezone().isoformat()
        # append reason to text for visibility if provided
        if reason:
            try:
                cur.execute('UPDATE notifications SET status=%s, attempts=attempts+1, last_attempt=%s, text = COALESCE(text, \'\') || %s WHERE id=%s', ('failed', now, '\nFailure reason: ' + reason, row_id))
            except Exception:
                cur.execute('UPDATE notifications SET status=%s, attempts=attempts+1, last_attempt=%s WHERE id=%s', ('failed', now, row_id))
        else:
            cur.execute('UPDATE notifications SET status=%s, attempts=attempts+1, last_attempt=%s WHERE id=%s', ('failed', now, row_id))
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_last_sent_time(user_id: str, db_path: str = DB_PATH) -> Optional[datetime]:
    """Return the last_attempt datetime for the most recent sent notification for user_id, or None."""
    conn = _get_conn(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT last_attempt FROM notifications WHERE user_id=%s AND status IN ('sent','dry','reserved','pending','processing') AND last_attempt IS NOT NULL ORDER BY last_attempt DESC LIMIT 1", (user_id,))
        r = cur.fetchone()
        if not r or not r[0]:
            return None
        val = r[0]
        # If the DB driver already returned a datetime, use it
        if isinstance(val, datetime):
            dt = val
        else:
            try:
                dt = datetime.fromisoformat(str(val))
            except Exception:
                return None
        # Ensure timezone-aware for comparisons
        if dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            except Exception:
                pass
        return dt
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_global_last_sent_time(db_path: str = DB_PATH) -> Optional[datetime]:
    """Return the most recent last_attempt datetime for any sent notification, or None."""
    conn = _get_conn(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT last_attempt FROM notifications WHERE status IN ('sent','dry') AND last_attempt IS NOT NULL ORDER BY last_attempt DESC LIMIT 1")
        r = cur.fetchone()
        if not r or not r[0]:
            return None
        val = r[0]
        if isinstance(val, datetime):
            dt = val
        else:
            try:
                dt = datetime.fromisoformat(str(val))
            except Exception:
                return None
        if dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            except Exception:
                pass
        return dt
    finally:
        try:
            conn.close()
        except Exception:
            pass
