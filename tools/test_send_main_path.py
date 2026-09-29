"""Test program that generates a notification via the same path as main.py.

Usage:
  python tools/test_send_main_path.py [--user USER_ID] [--send]

- Exercises the interactive pre-survey notification path.
- With --send, it calls notifier_worker.process_batch(do_send=True) to attempt
    actual Slack send.

Note: Ensure .env contains DATABASE_URL and Slack/OpenAI credentials if you want
real sending / AI generation.
"""
import os
import sys
from datetime import datetime, timezone

repo_root = os.path.dirname(os.path.dirname(__file__))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

# load .env if present
env_path = os.path.join(repo_root, '.env')
if os.path.exists(env_path):
    with open(env_path, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import argparse
import logging
import storage_sqlite
# additional test helpers: import only the functions we need so missing Slack SDK
# does not block running this test.
try:
    from send_slack_checkbox import save_survey_result, _generate_exercise_payload
    _have_send_slack_checkbox = True
except Exception:
    try:
        import importlib
        send_slack_checkbox = importlib.import_module('send_slack_checkbox')
        save_survey_result = getattr(send_slack_checkbox, 'save_survey_result', None)
        _generate_exercise_payload = getattr(send_slack_checkbox, '_generate_exercise_payload', None)
        _have_send_slack_checkbox = bool(save_survey_result and _generate_exercise_payload)
    except Exception:
        save_survey_result = None
        _generate_exercise_payload = None
        _have_send_slack_checkbox = False

logging.basicConfig(level=logging.INFO)

p = argparse.ArgumentParser()
p.add_argument('--user', '-u', default=os.environ.get('TEST_USER') or 'seiichirou019@gmail.com')
p.add_argument('--send', action='store_true', help='Attempt actual Slack send by running notifier_worker')
args = p.parse_args()

user = args.user
print('Test user:', user)

# This test focuses on no_movement -> interactive pre-survey flow

# Ensure schema (no-op if already applied)
storage_sqlite.ensure_schema()

try:
    # Prefer to call launch_session to trigger the interactive pre-survey flow.
    try:
        import send_slack_checkbox
        # launch_session signature: (user_id, channel, motion_detected, exercise_type, ai_context=None, detection_type=None)
        recommended_types = ['stretch', 'cardio', 'strength']  # Example recommended types
        types = str(recommended_types)
        send_slack_checkbox.launch_session(user, None, False, types, ai_context=None, detection_type='no_movement')
        print('send_slack_checkbox.launch_session invoked')
    except Exception as e:
        print('launch_session failed, falling back to enqueue pre-survey blocks:', e)
        # fallback: enqueue pre-survey blocks directly so notifier posts interactive UI
        try:
            from send_slack_checkbox import build_pre_survey_blocks
            pre_blocks = build_pre_survey_blocks()
        except Exception:
            pre_blocks = None
        if pre_blocks:
            try:
                import json as _json
                try:
                    nid2 = storage_sqlite.create_sent_notification(user, 'Pre-survey', 'survey', 'アンケート: 人目に付きますか？')
                    storage_sqlite.create_pending_response(nid2, user)
                except Exception:
                    nid2 = None
                enqid = storage_sqlite.enqueue_notification_for_user(user, 'アンケート: 人目に付きますか？', notification_id=nid2, payload=_json.dumps(pre_blocks, ensure_ascii=False))
                print('Enqueued pre-survey blocks id:', enqid)
            except Exception as e2:
                print('Enqueue pre-survey failed:', e2)
        else:
            print('No pre-survey blocks available to enqueue')
except Exception as e:
    print('Interactive pre-survey test failed:', e)

# No dispatch of 'increase' events in this test; interactive pre-survey flow only

# Optionally attempt to send now
if args.send:
    print('\nRunning notifier_worker to send...')
    try:
        import notifier_worker
        sent = notifier_worker.process_batch(limit=10, do_send=True)
        print('notifier_worker sent count:', sent)
    except Exception as e:
        print('Failed to run notifier_worker:', e)

print('Done')
