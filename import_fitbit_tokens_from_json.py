"""Authorize Google Health API and save its credentials in fitbit_tokens.

Usage:
    python import_fitbit_tokens_from_json.py --json fitbit_token.json --user-id user@example.com

The JSON file is the OAuth client configuration downloaded from Google Cloud.
This program opens a browser, receives the local OAuth callback, and stores the
resulting Google Health access and refresh tokens in the existing token table.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import timezone
from typing import Iterable, Sequence

from db import connect as db_connect
from google_auth_oauthlib.flow import InstalledAppFlow


DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
)


def ensure_fitbit_tokens_schema() -> list[str]:
    """Add fields required to distinguish legacy Fitbit and Google Health OAuth."""
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fitbit_tokens (
            user_id TEXT PRIMARY KEY,
            access_token TEXT,
            refresh_token TEXT,
            token_type TEXT,
            scope TEXT,
            expires_at BIGINT,
            obtained_at BIGINT,
            client_id TEXT,
            client_secret TEXT,
            oauth_type TEXT
        )
        """
    )
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'fitbit_tokens'
        """
    )
    existing = {str(row[0]) for row in cur.fetchall() if row and row[0]}
    required = {
        "token_type": "TEXT",
        "scope": "TEXT",
        "obtained_at": "BIGINT",
        "client_id": "TEXT",
        "client_secret": "TEXT",
        "oauth_type": "TEXT",
    }
    added = []
    for column, column_type in required.items():
        if column not in existing:
            cur.execute(f"ALTER TABLE fitbit_tokens ADD COLUMN {column} {column_type}")
            added.append(column)
    return added


def _read_client_config(json_path: str) -> tuple[str, str | None]:
    with open(json_path, "r", encoding="utf-8") as file:
        payload = json.load(file)
    client_config = payload.get("installed") or payload.get("web")
    if not isinstance(client_config, dict) or not client_config.get("client_id"):
        raise ValueError(
            "Google Cloud OAuth client JSON must include an 'installed' or 'web' object with client_id."
        )
    client_id = str(client_config["client_id"])
    client_secret = client_config.get("client_secret")
    return client_id, str(client_secret) if client_secret else None


def authorize_google_health(json_path: str, scopes: Sequence[str]):
    flow = InstalledAppFlow.from_client_secrets_file(json_path, scopes=list(scopes))
    return flow.run_local_server(
        host="localhost",
        port=0,
        authorization_prompt_message="Google Health authorization will open in your browser.\n",
        success_message="Authorization completed. You may close this tab.",
        open_browser=True,
        access_type="offline",
        prompt="consent",
    )


def save_google_health_credentials(
    user_id: str,
    client_id: str,
    client_secret: str | None,
    credentials,
    scopes: Iterable[str],
) -> None:
    if not credentials.token or not credentials.refresh_token:
        raise RuntimeError(
            "Google did not return both access_token and refresh_token. "
            "Re-run the flow and complete the consent screen."
        )

    now = int(time.time())
    expires_at = None
    if credentials.expiry:
        expires_at = int(credentials.expiry.astimezone(timezone.utc).timestamp())

    db_connect().execute(
        """
        INSERT INTO fitbit_tokens(
            user_id, access_token, refresh_token, token_type, scope,
            expires_at, obtained_at, client_id, client_secret, oauth_type
        )
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            token_type=excluded.token_type,
            scope=excluded.scope,
            expires_at=excluded.expires_at,
            obtained_at=excluded.obtained_at,
            client_id=excluded.client_id,
            client_secret=excluded.client_secret,
            oauth_type=excluded.oauth_type
        """,
        (
            user_id,
            credentials.token,
            credentials.refresh_token,
            "Bearer",
            " ".join(scopes),
            expires_at,
            now,
            client_id,
            client_secret,
            "google_health",
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize Google Health API and save tokens to fitbit_tokens."
    )
    parser.add_argument("--json", required=True, help="Google Cloud OAuth client JSON path")
    parser.add_argument("--user-id", required=True, help="fitbit_tokens user_id")
    parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        help="Additional Google Health OAuth scope; specify more than once as needed",
    )
    args = parser.parse_args()

    scopes = tuple(dict.fromkeys([*DEFAULT_SCOPES, *(args.scopes or [])]))
    try:
        added = ensure_fitbit_tokens_schema()
        client_id, client_secret = _read_client_config(args.json)
        credentials = authorize_google_health(args.json, scopes)
        save_google_health_credentials(
            args.user_id, client_id, client_secret, credentials, scopes
        )
    except Exception as error:
        print(f"ERROR: {error}")
        return 1

    print(f"Google Health authorization saved for user_id={args.user_id}")
    print("Added missing columns:", ", ".join(added) if added else "none")
    print("Granted scopes:", " ".join(scopes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())