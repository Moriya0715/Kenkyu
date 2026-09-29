import os
from pathlib import Path


def load_env_file(path: str | None = None) -> None:
    env_path = Path(path) if path else Path(__file__).with_name('.env')
    if not env_path.exists():
        return

    with env_path.open(encoding='utf-8') as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)