-- PostgreSQL schema for migrating from SQLite
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- fitbit tokens
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

-- slack mapping
CREATE TABLE IF NOT EXISTS slack_id (
  user_id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL,
  note TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_slack_user ON slack_id(user_id);

-- sent exercise notifications
CREATE TABLE IF NOT EXISTS sent_exercise_notifications (
  id SERIAL PRIMARY KEY,
  notification_id TEXT UNIQUE,
  user_id TEXT NOT NULL,
  exercise_name TEXT NOT NULL,
  exercise_type TEXT NOT NULL,
  sent_at TIMESTAMPTZ,
  reps INTEGER,
  sets INTEGER,
  duration_min INTEGER,
  ai_category TEXT,
  ai_rationale TEXT,
  message_text TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ai_context TEXT,
  detection_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_sent_user_sent_at ON sent_exercise_notifications (user_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_sent_updated_at ON sent_exercise_notifications (updated_at);

-- received exercise responses
CREATE TABLE IF NOT EXISTS received_exercise_responses (
  id SERIAL PRIMARY KEY,
  notification_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  response_status TEXT NOT NULL,
  implemented_flag INTEGER,
  not_implemented_reason TEXT,
  visibility_before INTEGER,
  perceived_exertion INTEGER,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_received_notification_id ON received_exercise_responses (notification_id);
CREATE INDEX IF NOT EXISTS idx_received_user_updated ON received_exercise_responses (user_id, updated_at);

-- exercise adherence stats
CREATE TABLE IF NOT EXISTS exercise_adherence_stats (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  presented_count INTEGER NOT NULL,
  implemented_count INTEGER NOT NULL,
  adherence_rate DOUBLE PRECISION NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, scope, period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_adherence_user_period ON exercise_adherence_stats (user_id, period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_adherence_scope_period ON exercise_adherence_stats (scope, period_start, period_end);

-- notifications queue
CREATE TABLE IF NOT EXISTS notifications (
  id SERIAL PRIMARY KEY,
  notification_id TEXT,
  user_id TEXT,
  channel TEXT,
  text TEXT,
  payload TEXT,
  message_ts TEXT,
  sent_at TIMESTAMPTZ,
  status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ,
  last_attempt TIMESTAMPTZ,
  notification_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_key ON notifications(notification_key);

-- user notification state (for dedup/rate limit)
CREATE TABLE IF NOT EXISTS user_notification_state (
  user_id TEXT PRIMARY KEY,
  last_decision_at BIGINT
);

-- google calendar tokens
CREATE TABLE IF NOT EXISTS google_calendar_tokens (
  account_id TEXT PRIMARY KEY,
  access_token TEXT,
  refresh_token TEXT,
  scope TEXT,
  token_type TEXT,
  expiry TIMESTAMPTZ,
  raw_json TEXT
);

-- named locks with heartbeat (see lock_db.py)
CREATE TABLE IF NOT EXISTS locks (
  lock_name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
