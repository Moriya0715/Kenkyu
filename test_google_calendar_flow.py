#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_google_calendar_flow.py - Google Calendar のスケジュール取得検証
"""
import os
from datetime import datetime

user_id = 'seiichirou019@gmail.com'

print('=' * 60)
print('Google Calendar スケジュール取得フロー検証')
print('=' * 60)

# Step 1: クレデンシャル確認
print('\n[Step 1] クレデンシャル確認...')
if os.path.exists('credentials.json'):
    print('✓ OK: credentials.json が存在')
else:
    print('✗ NG: credentials.json が見つかりません')
    print('  必要なファイル: credentials.json (Google Cloud Console で取得)')
    exit(1)

if os.path.exists('token.json'):
    print('✓ OK: token.json が存在')
else:
    print('✗ NG: token.json が見つかりません')
    print('  初回実行時にブラウザで認可してください')

# Step 2: google_calendar_freebusy.main() を実行
print('\n[Step 2] Google Calendar から free/busy を取得...')
try:
    import google_calendar_freebusy
    google_calendar_freebusy.main(account_id=user_id)
    print(f'✓ OK: google_calendar_freebusy.main() 実行成功')
except Exception as e:
    import traceback
    print(f'✗ NG: {e}')
    traceback.print_exc()
    exit(1)

# Step 3: 出力ファイルを確認
print('\n[Step 3] 出力ファイルの確認...')
try:
    import storage_sqlite
    safe = storage_sqlite._sanitize_user_id(user_id)
    today = datetime.now().strftime('%Y-%m-%d')
    out_dir = os.path.join('data_output', safe, 'date')
    out_file = os.path.join(out_dir, f'freebusy_{today}.csv')
    
    if os.path.exists(out_file):
        print(f'✓ OK: ファイルが出力されました')
        print(f'  出力先: {out_file}')
        
        # ファイル内容を確認
        with open(out_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            print(f'  行数: {len(lines)}')
            if len(lines) > 1:
                print(f'  ヘッダ: {lines[0].strip()}')
                print(f'  データ行数: {len(lines) - 1}')
                if len(lines) > 1:
                    print(f'  最初のデータ: {lines[1].strip()[:60]}...')
    else:
        print(f'✗ NG: ファイルが見つかりません')
        print(f'  期待されるパス: {out_file}')
        
        # 実は別の場所に出力されているか確認
        import glob
        pattern = os.path.join('data_output', '**', 'date', f'freebusy_{today}.csv')
        files = glob.glob(pattern, recursive=True)
        if files:
            print(f'  見つかった freebusy ファイル:')
            for f in files:
                print(f'    {f}')
        exit(1)
        
except Exception as e:
    import traceback
    print(f'✗ NG: {e}')
    traceback.print_exc()
    exit(1)

print('\n' + '=' * 60)
print('✓ Google Calendar スケジュール取得が正常に動作しています')
print('=' * 60)
