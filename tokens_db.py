"""tokens_db.py

仕様:
- テーブル: slack_id
  - user_id TEXT PRIMARY KEY
  - channel_id TEXT NOT NULL
  - note TEXT NULL
  - updated_at TEXT NOT NULL

このモジュールはマイグレーション（テーブル作成）と簡易 CRUD を提供します。
DB接続は db.py の connect() に統一し、生SQLで操作します。
"""

from typing import Optional, List, Dict
from datetime import datetime

from db import connect as db_connect


def ensure_schema() -> None:
    con = db_connect()
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_id (
            user_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            note TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def set_channel_for_user(user_id: str, channel_id: str, note: Optional[str] = None) -> None:
    """Insert or update mapping for user_id -> channel_id using upsert."""
    ensure_schema()
    now = datetime.now().isoformat()
    con = db_connect()
    con.execute(
        """
        INSERT INTO slack_id (user_id, channel_id, note, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
          channel_id = excluded.channel_id,
          note = excluded.note,
          updated_at = excluded.updated_at
        """,
        (user_id, channel_id, note, now),
    )
    con.commit()
    con.close()


def get_channel_for_user(user_id: str) -> Optional[str]:
    ensure_schema()
    con = db_connect()
    cur = con.execute("SELECT channel_id FROM slack_id WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None
