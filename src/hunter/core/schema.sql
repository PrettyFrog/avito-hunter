PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS listings (
  avito_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT,
  price INTEGER,
  description TEXT,
  params TEXT,
  city TEXT,
  delivery INTEGER,
  seller_id TEXT,
  seller_type TEXT,
  watchlist_id TEXT,
  raw_path TEXT,
  fingerprint TEXT,
  price_suspect INTEGER DEFAULT 0,
  published_at TEXT
);

CREATE TABLE IF NOT EXISTS listing_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  avito_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT,
  FOREIGN KEY (avito_id) REFERENCES listings(avito_id)
);

CREATE TABLE IF NOT EXISTS price_history (
  listing_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  price INTEGER NOT NULL,
  PRIMARY KEY (listing_id, ts)
);

CREATE TABLE IF NOT EXISTS images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  avito_id TEXT NOT NULL,
  url TEXT NOT NULL,
  local_path TEXT,
  phash TEXT
);

CREATE TABLE IF NOT EXISTS sellers (
  seller_id TEXT PRIMARY KEY,
  kind TEXT,
  rating REAL,
  reviews INTEGER,
  registered TEXT,
  listings_count INTEGER,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
  day TEXT PRIMARY KEY,
  requests INTEGER DEFAULT 0,
  http_429 INTEGER DEFAULT 0,
  http_403 INTEGER DEFAULT 0,
  parse_ok INTEGER DEFAULT 0,
  parse_fail INTEGER DEFAULT 0,
  hits_total INTEGER DEFAULT 0,
  hits_no_price INTEGER DEFAULT 0,
  anchor_fallback INTEGER DEFAULT 0,
  empty_ok INTEGER DEFAULT 0,
  unknown_empty INTEGER DEFAULT 0,
  carousel_discarded INTEGER DEFAULT 0,
  shadow_cards_rejected INTEGER DEFAULT 0,
  light_ok INTEGER DEFAULT 0,
  light_fallbacks INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runtime (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  product_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  payload TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  next_run_at TEXT,
  lease_until TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_dedupe 
  ON jobs(kind, dedupe_key) 
  WHERE status IN ('queued', 'leased');

CREATE INDEX IF NOT EXISTS idx_jobs_fetch 
  ON jobs(status, priority DESC, id ASC);

CREATE TABLE IF NOT EXISTS request_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  product_id TEXT,
  req_type TEXT NOT NULL,
  status INTEGER,
  url TEXT,
  latency_ms INTEGER DEFAULT 0,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts);

CREATE INDEX IF NOT EXISTS idx_request_log_product ON request_log(product_id, ts);

CREATE TABLE IF NOT EXISTS health_daily (
  date TEXT PRIMARY KEY,
  total_reqs INTEGER DEFAULT 0,
  err_429 INTEGER DEFAULT 0,
  err_403 INTEGER DEFAULT 0,
  captcha_count INTEGER DEFAULT 0,
  timeout_count INTEGER DEFAULT 0,
  timeout_ratio REAL DEFAULT 0.0,
  latency_median_ms REAL DEFAULT 0.0,
  latency_p90_ms REAL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  call_type TEXT NOT NULL,
  product_id TEXT,
  avito_id TEXT,
  tokens_prompt INTEGER DEFAULT 0,
  tokens_completion INTEGER DEFAULT 0,
  tokens_total INTEGER DEFAULT 0,
  latency_ms INTEGER DEFAULT 0,
  status TEXT DEFAULT 'ok',
  error_msg TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);

CREATE INDEX IF NOT EXISTS idx_llm_calls_product ON llm_calls(product_id, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS listings_fts USING fts5(
  avito_id UNINDEXED,
  title,
  description,
  params,
  tokenize = 'unicode61'
);

CREATE TRIGGER IF NOT EXISTS listings_ai AFTER INSERT ON listings BEGIN
  INSERT INTO listings_fts(avito_id, title, description, params)
  VALUES (new.avito_id, COALESCE(new.title, ''), COALESCE(new.description, ''), COALESCE(new.params, ''));

END;

CREATE TRIGGER IF NOT EXISTS listings_ad AFTER DELETE ON listings BEGIN
  DELETE FROM listings_fts WHERE avito_id = old.avito_id;

END;

CREATE TRIGGER IF NOT EXISTS listings_au AFTER UPDATE ON listings BEGIN
  DELETE FROM listings_fts WHERE avito_id = old.avito_id;

INSERT INTO listings_fts(avito_id, title, description, params)
  VALUES (new.avito_id, COALESCE(new.title, ''), COALESCE(new.description, ''), COALESCE(new.params, ''));

END;

CREATE TABLE IF NOT EXISTS pending_products (
  chat_id INTEGER PRIMARY KEY,
  step TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  owner_user_id INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_user_id INTEGER UNIQUE,
  chat_id INTEGER,
  username TEXT,
  display_name TEXT,
  role TEXT NOT NULL DEFAULT 'user',
  status TEXT NOT NULL DEFAULT 'active',
  tier TEXT NOT NULL DEFAULT 'friend',
  max_products INTEGER DEFAULT 3,
  min_interval_minutes INTEGER DEFAULT 15,
  max_alerts_per_hour INTEGER DEFAULT 10,
  quiet_start INTEGER,
  quiet_end INTEGER,
  timezone TEXT DEFAULT 'Europe/Moscow',
  invited_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(telegram_user_id);

CREATE TABLE IF NOT EXISTS invites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT UNIQUE NOT NULL,
  label TEXT,
  tier TEXT NOT NULL DEFAULT 'friend',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  max_uses INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0,
  used_at TEXT,
  revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS access_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_user_id INTEGER NOT NULL,
  username TEXT,
  display_name TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  decided_by INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL,
  ts TEXT NOT NULL,
  details TEXT
);

CREATE INDEX IF NOT EXISTS idx_user_events_user_ts ON user_events(user_id, ts);

CREATE TABLE IF NOT EXISTS digest_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  avito_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_digest_queue_user ON digest_queue(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_user_events_rate ON user_events(user_id, kind, ts);

CREATE TABLE IF NOT EXISTS support_tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  chat_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  answered_at TEXT,
  answered_by INTEGER REFERENCES users(id),
  response_text TEXT,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status, created_at);

CREATE TABLE IF NOT EXISTS broadcasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender_user_id INTEGER REFERENCES users(id),
  target_audience TEXT NOT NULL,
  text TEXT NOT NULL,
  delivered_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_broadcasts_created ON broadcasts(created_at);

CREATE INDEX IF NOT EXISTS idx_listing_events_avito_id ON listing_events(avito_id);

CREATE INDEX IF NOT EXISTS idx_listing_events_ts ON listing_events(ts);

CREATE INDEX IF NOT EXISTS idx_listings_watchlist_id ON listings(watchlist_id);

CREATE INDEX IF NOT EXISTS idx_images_avito_id ON images(avito_id);

-- v12/v13: subscriptions, events and delivery outbox (S3 / T-459, T-460)
CREATE TABLE IF NOT EXISTS queries (
    id TEXT PRIMARY KEY,
    canonical_url TEXT UNIQUE NOT NULL,
    contract_version INTEGER NOT NULL DEFAULT 1,
    bootstrap_state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    query_id TEXT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    max_price INTEGER NOT NULL,
    manual_stopwords TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    manual_keywords TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    ai_enrichment_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, query_id)
);

CREATE TABLE IF NOT EXISTS query_membership (
    query_id TEXT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    avito_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    baseline_epoch INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (query_id, avito_id)
);

CREATE TABLE IF NOT EXISTS subscription_seen (
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    avito_id TEXT NOT NULL,
    baseline_epoch INTEGER NOT NULL DEFAULT 0,
    last_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (subscription_id, avito_id)
);

CREATE TABLE IF NOT EXISTS listing_state (
    avito_id TEXT PRIMARY KEY,
    known_price INTEGER,
    price_state TEXT NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1,
    last_request_sequence INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    avito_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('NEW_LISTING', 'PRICE_CHANGED')),
    observed_at TEXT NOT NULL,
    old_price INTEGER,
    new_price INTEGER,
    origin TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(avito_id, revision, kind, origin)
);

CREATE TABLE IF NOT EXISTS recipient_events (
    user_id INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, event_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    destination_version INTEGER NOT NULL DEFAULT 1,
    payload_version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    available_at TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until TEXT,
    message_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, event_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_events_avito ON events(avito_id, revision);

CREATE INDEX IF NOT EXISTS idx_deliveries_due ON deliveries(status, available_at);

CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    cookie_namespace TEXT NOT NULL,
    health_state TEXT NOT NULL DEFAULT 'healthy',
    quarantine_until TEXT,
    active_generation INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL DEFAULT 'active' CHECK(role IN ('active', 'reserve')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    query_id TEXT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0 AND position <= 3),
    generation INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(slot_id, position),
    UNIQUE(slot_id, generation)
);

CREATE TABLE IF NOT EXISTS session_versions (
    slot_id TEXT PRIMARY KEY REFERENCES slots(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    cookie_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channel_destinations (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('telegram', 'vk')),
    address TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_assignments_slot_active ON assignments(slot_id, active);

CREATE INDEX IF NOT EXISTS idx_assignments_query ON assignments(query_id);

CREATE INDEX IF NOT EXISTS idx_channel_destinations_user ON channel_destinations(user_id);

CREATE TABLE IF NOT EXISTS query_groups (
    id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    filter_fingerprint TEXT NOT NULL,
    max_price INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS group_members (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES query_groups(id) ON DELETE CASCADE,
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    personal_max_price INTEGER NOT NULL,
    stopwords TEXT NOT NULL DEFAULT '[]',
    generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(group_id, subscription_id)
);

CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);

CREATE INDEX IF NOT EXISTS idx_group_members_sub ON group_members(subscription_id);

CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);

CREATE TABLE IF NOT EXISTS price_watch_jobs (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    query_group_id TEXT REFERENCES query_groups(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    lease_token TEXT,
    lease_until TEXT,
    next_run_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_price_watch_jobs_due ON price_watch_jobs(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_price_watch_jobs_lease ON price_watch_jobs(lease_token) WHERE lease_token IS NOT NULL;

