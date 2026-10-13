"""Authorize Google Calendar and save credentials in google_calendar_tokens.

Usage:
    python import_google_calendar_tokens_from_json.py --json google_calendar.json --account-id user@example.com

The JSON file is the OAuth client configuration downloaded from Google Cloud.
This program opens a browser, receives the local OAuth callback, and stores the
resulting Google Calendar access and refresh tokens in the existing token table.
"""

from __future__ import annotations

import argparse

from db import connect as db_connect
from google_auth_oauthlib.flow import InstalledAppFlow


DEFAULT_SCOPES = ("https://www.googleapis.com/auth/calendar.readonly",)


def ensure_google_calendar_tokens_schema() -> None:
    """Create the Google Calendar token table if it does not already exist."""
    conn = db_connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS google_calendar_tokens (
                account_id TEXT PRIMARY KEY,
                access_token TEXT,
                refresh_token TEXT,
                scope TEXT,
                token_type TEXT,
                expiry TIMESTAMPTZ,
                raw_json TEXT
            )
            """
        )
    finally:
        conn.close()


def authorize_google_calendar(json_path: str, scopes: tuple[str, ...]):
    flow = InstalledAppFlow.from_client_secrets_file(json_path, scopes=list(scopes))
    return flow.run_local_server(
        host="localhost",
        port=0,
        authorization_prompt_message="Google Calendar authorization will open in your browser.\n",
        success_message="Authorization completed. You may close this tab.",
        open_browser=True,
        access_type="offline",
        prompt="consent",
    )


def save_google_calendar_credentials(
    account_id: str,
    credentials,
    scopes: tuple[str, ...],
) -> None:
    if not credentials.token or not credentials.refresh_token:
        raise RuntimeError(
            "Google did not return both access_token and refresh_token. "
            "Re-run the flow and complete the consent screen."
        )

    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO google_calendar_tokens(
                account_id, access_token, refresh_token, scope, token_type, expiry, raw_json
            )
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                scope=excluded.scope,
                token_type=excluded.token_type,
                expiry=excluded.expiry,
                raw_json=excluded.raw_json
            """,
            (
                account_id,
                credentials.token,
                credentials.refresh_token,
                " ".join(credentials.scopes or scopes),
                "Bearer",
                credentials.expiry.isoformat() if credentials.expiry else None,
                credentials.to_json(),
            ),
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize Google Calendar and save tokens to google_calendar_tokens."
    )
    parser.add_argument("--json", required=True, help="Google Cloud OAuth client JSON path")
    parser.add_argument(
        "--account-id",
        required=True,
        help="Google account ID (email) used as google_calendar_tokens.account_id",
    )
    args = parser.parse_args()

    account_id = args.account_id.strip()
    if not account_id:
        parser.error("--account-id must not be empty")

    try:
        ensure_google_calendar_tokens_schema()
        credentials = authorize_google_calendar(args.json, DEFAULT_SCOPES)
        save_google_calendar_credentials(account_id, credentials, DEFAULT_SCOPES)
    except Exception as error:
        print(f"ERROR: {error}")
        return 1

    print(f"Google Calendar authorization saved for account_id={account_id}")
    print("Granted scopes:", " ".join(DEFAULT_SCOPES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
