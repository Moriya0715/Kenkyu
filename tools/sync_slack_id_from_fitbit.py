"""fitbit_tokens にあって slack_id にいないユーザを slack_id に挿入する。

channel_id は NOT NULL のため空文字 '' を入れる。
使い方:
    python tools/sync_slack_id_from_fitbit.py [--dry-run]
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import connect  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="挿入せず対象を表示のみ")
    args = parser.parse_args()

    con = connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT user_id FROM fitbit_tokens "
            "WHERE user_id IS NOT NULL AND user_id <> '' "
            "AND user_id NOT IN (SELECT user_id FROM slack_id)"
        ).fetchall()
        missing = [r[0] for r in rows]
        if not missing:
            print("不足しているユーザはいません。")
            return
        for uid in missing:
            print(("[dry-run] " if args.dry_run else "") + f"insert: {uid}")
            if not args.dry_run:
                con.execute(
                    "INSERT INTO slack_id (user_id, channel_id, note, updated_at) "
                    "VALUES (?, '', ?, ?) ON CONFLICT (user_id) DO NOTHING",
                    (uid, "auto-added from fitbit_tokens", datetime.now().isoformat()),
                )
        con.commit()
        print(f"{len(missing)} 件{'が対象です' if args.dry_run else '挿入しました'}。")
    finally:
        con.close()


if __name__ == "__main__":
    main()
