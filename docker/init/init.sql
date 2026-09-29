-- 初期化 SQL: 必要ならスキーマや拡張をここで作成
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- サンプルテーブル（必要に応じて調整してください）
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
);
