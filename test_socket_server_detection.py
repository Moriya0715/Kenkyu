#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test launch_session socket-server path detection."""
import os
import sys
import logging

# Setup logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s',
)

# Check socket_server.lock
socket_server_lock = os.path.join(os.getcwd(), 'locks', 'socket_server.lock')
lock_exists = os.path.exists(socket_server_lock)
print(f'socket_server.lock exists: {lock_exists}')

if lock_exists:
    with open(socket_server_lock, 'r') as f:
        pid = f.read().strip()
    print(f'socket_server PID: {pid}')
    
    # Check if process is running
    try:
        import subprocess
        result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], capture_output=True, text=True)
        if pid in result.stdout:
            print(f'Process {pid} is RUNNING ✓')
        else:
            print(f'Process {pid} is NOT running ✗')
    except Exception as e:
        print(f'Error checking process: {e}')

print('\nTesting launch_session behavior...')

# Set env and import
os.environ.pop('USE_SOCKET_SERVER', None)  # Ensure it's not explicitly set
import send_slack_checkbox
import storage_sqlite

# Test socket_server_running detection inside launch_session
print(f'socket_server_running (lock exists): {lock_exists}')

# Create a test entry in DB to verify enqueue works
try:
    user_id = 'test_socket_server@gmail.com'
    nid = storage_sqlite.create_sent_notification(
        user_id=user_id,
        exercise_name='Test Exercise',
        exercise_type='stretch',
        message_text='Test Message',
        ai_context={},
    )
    print(f'Created test notification ID: {nid}')
    
    # Verify it was created
    conn = storage_sqlite._get_conn(storage_sqlite.DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT id, user_id, status FROM sent_exercise_notifications WHERE id = %s', (nid,))
    row = cur.fetchone()
    conn.close()
    
    if row:
        print(f'Verified: ID={row[0]} User={row[1]} Status={row[2]}')
    else:
        print('ERROR: Notification not found after creation')
        
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()

print('\n✓ Test completed')
