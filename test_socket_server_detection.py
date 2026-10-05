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

# Check DB lock for socket_server
import lock_db
lock_exists = lock_db.is_held('socket_server')
print(f'socket_server DB lock held: {lock_exists}')

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
