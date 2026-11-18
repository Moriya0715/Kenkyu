#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Enqueue a manual pre-survey flow for a test user.

Run socket_server.py separately. This script only starts the same
launch_session path used by the main program; Slack interaction and AI
message updates are handled by socket_server.py and send_slack_checkbox.py.
"""
from __future__ import annotations

import argparse
import os
import sys


DEFAULT_USER_ID = 'seiichiro019@gmail.com'


def _load_dotenv() -> None:
    """Load repository .env values before importing DB-dependent modules."""
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(dotenv_path):
        return
    with open(dotenv_path, 'r', encoding='utf-8') as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Enqueue the production-like pre-survey flow for a test user.'
    )
    parser.add_argument(
        '--user-id',
        default=DEFAULT_USER_ID,
        help='User ID used to resolve the Slack destination from the DB.',
    )
    args = parser.parse_args()

    _load_dotenv()

    import lock_db
    import send_slack_checkbox

    if not lock_db.is_held('socket_server'):
        raise RuntimeError(
            'socket_server.py must be running before this test. '
            'The test will not start another Socket Mode process.'
        )

    send_slack_checkbox.launch_session(
        user_id=args.user_id,
        channel=None,
        motion_detected=False,
        exercise_type='stretch',
        ai_context={'test_name': 'socket_server_pre_survey'},
        detection_type='no_movement',
    )
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'test setup failed: {exc}', file=sys.stderr)
        sys.exit(1)
