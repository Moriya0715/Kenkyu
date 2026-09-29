"""Display the active Google Health OAuth token expiry without exposing credentials."""

from datetime import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import get_fitbit_token


USER_ID = "seiichirou019@gmail.com"


if __name__ == "__main__":
    access_token, expires_at, _ = get_fitbit_token.get_cached_access_token(USER_ID)
    print("Google Health access token is available:", bool(access_token))
    print("Expires at:", datetime.fromtimestamp(expires_at).astimezone().isoformat())
