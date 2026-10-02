-- Superset of the columns every writer and reader in this repo uses.
-- Chaos Smart Wallet Tracker schema
-- Checked against the secondary export shapes.
-- Boundary: local read-only intelligence ledger. No execution, alerts, posting, or signing.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES
  ('schema_name', 'chaos_smart_wallet_tracker', CURRENT_TIMESTAMP),
  ('schema_version', '0.2_secondary', CURRENT_TIMESTAMP),
  ('boundary', 'local intelligence ledger only; no trading/signing/alerts/posting', CURRENT_TIMESTAMP);

-- One row per source system/export/feed/account.
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL, -- secondary, helius, x, telegram_channel, manual, wallet_api, rpc
  display_name TEXT,
  handle TEXT,
  url TEXT,
  trust_score REAL,
  status TEXT DEFAULT 'active', -- active, review, decayed, banned
  metadata_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Secondary export pulls/imports and future Helius batch pulls.
CREATE TABLE IF NOT EXISTS ingestion_runs (
  run_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  source_path TEXT,
  source_commit TEXT,
  started_at TEXT,
  completed_at TEXT,
  row_counts_json TEXT,
  status TEXT DEFAULT 'completed',
  notes TEXT,
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

-- Wallet is a node. Do not assume one wallet equals one actor.
CREATE TABLE IF NOT EXISTS wallets (
  address TEXT PRIMARY KEY,
  label TEXT,
  handle TEXT,
  display_name TEXT,
  identity_name TEXT,
  identity_source TEXT,
  identity_type TEXT,
  category TEXT,
  first_seen_utc TEXT,
  last_seen_utc TEXT,
  sol_balance REAL,
  total_usd_value REAL,
  token_count INTEGER,
  top_holding_symbol TEXT,
  top_holding_usd REAL,
  status TEXT DEFAULT 'active',
  notes TEXT,
  metadata_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Actor cluster groups wallets probably controlled by the same operator or strategy group.
CREATE TABLE IF NOT EXISTS actors (
  actor_id TEXT PRIMARY KEY,
  label TEXT,
  primary_wallet TEXT,
  handle TEXT,
  classification TEXT, -- copyable_candidate, high_churn_scout, deep_watch, insider_risk, noisy, unknown
  confidence TEXT DEFAULT 'low',
  reason_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(primary_wallet) REFERENCES wallets(address)
);

CREATE TABLE IF NOT EXISTS actor_wallets (
  actor_id TEXT NOT NULL,
  wallet TEXT NOT NULL,
  link_type TEXT NOT NULL, -- primary, funded, transfer_linked, shared_funder, overlap_cluster, manual
  confidence TEXT DEFAULT 'medium',
  evidence_json TEXT,
  first_seen_utc TEXT,
  last_seen_utc TEXT,
  PRIMARY KEY(actor_id, wallet, link_type),
  FOREIGN KEY(actor_id) REFERENCES actors(actor_id),
  FOREIGN KEY(wallet) REFERENCES wallets(address)
);

-- Generic graph edge table; covers direct transfers and secondary shared-mint cluster edges.
CREATE TABLE IF NOT EXISTS wallet_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_wallet TEXT NOT NULL,
  dst_wallet TEXT NOT NULL,
  edge_type TEXT NOT NULL, -- funded_by, sent_sol_to, received_sol_from, sent_token_to, shared_funder, shared_mint_overlap, coordinated_buy_window
  mint TEXT,
  signature TEXT,
  amount REAL,
  symbol TEXT,
  shared_mints INTEGER,
  observed_at_utc TEXT,
  confidence TEXT DEFAULT 'medium',
  source_id TEXT,
  run_id TEXT,
  metadata_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(src_wallet) REFERENCES wallets(address),
  FOREIGN KEY(dst_wallet) REFERENCES wallets(address),
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(run_id) REFERENCES ingestion_runs(run_id)
);

CREATE TABLE IF NOT EXISTS tokens (
  mint TEXT PRIMARY KEY,
  symbol TEXT,
  name TEXT,
  launchpad TEXT,
  deployer_wallet TEXT,
  first_seen_utc TEXT,
  graduated_at_utc TEXT,
  latest_market_cap_usd REAL,
  latest_holder_count INTEGER,
  latest_supply_pct REAL,
  metadata_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(deployer_wallet) REFERENCES wallets(address)
);

-- Raw tx cache for Helius/Solana expansion.
CREATE TABLE IF NOT EXISTS transactions (
  signature TEXT PRIMARY KEY,
  slot INTEGER,
  block_time_utc TEXT,
  fee_lamports INTEGER,
  err_json TEXT,
  source_id TEXT,
  raw_json TEXT,
  fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

-- Normalized secondary/Helius wallet events. This is the base fact table.
CREATE TABLE IF NOT EXISTS wallet_token_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wallet TEXT NOT NULL,
  mint TEXT,
  signature TEXT,
  block_time_utc TEXT,
  event_type TEXT NOT NULL, -- buy, sell, airdrop, token_transfer_in/out, sol_transfer_in/out, usdc_transfer_in/out, fee, tip, unknown
  side TEXT, -- Secondary original side where applicable
  token_delta REAL,
  sol_delta REAL,
  usdc_delta REAL,
  amount_token REAL,
  amount_sol REAL,
  amount_usd REAL,
  market_cap_usd REAL,
  fee_sol REAL,
  counterparty TEXT,
  route TEXT,
  source_id TEXT,
  run_id TEXT,
  confidence TEXT DEFAULT 'medium',
  metadata_json TEXT,
  FOREIGN KEY(wallet) REFERENCES wallets(address),
  FOREIGN KEY(mint) REFERENCES tokens(mint),
  FOREIGN KEY(signature) REFERENCES transactions(signature),
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(run_id) REFERENCES ingestion_runs(run_id)
);

-- Transfer-adjusted per-wallet/mint position ledger.
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wallet TEXT NOT NULL,
  mint TEXT NOT NULL,
  opened_at_utc TEXT,
  closed_at_utc TEXT,
  status TEXT DEFAULT 'open', -- open, closed, partial, unresolved
  buy_count INTEGER DEFAULT 0,
  sell_count INTEGER DEFAULT 0,
  sol_spent REAL DEFAULT 0,
  sol_received REAL DEFAULT 0,
  usdc_spent REAL DEFAULT 0,
  usdc_received REAL DEFAULT 0,
  transfer_in_sol REAL DEFAULT 0,
  transfer_out_sol REAL DEFAULT 0,
  transfer_in_tokens REAL DEFAULT 0,
  transfer_out_tokens REAL DEFAULT 0,
  fees_sol REAL DEFAULT 0,
  tips_sol REAL DEFAULT 0,
  realized_pnl_sol REAL,
  realized_pnl_usd REAL,
  remaining_tokens REAL,
  remaining_value_usd REAL,
  max_position_sol REAL,
  hold_seconds INTEGER,
  transfer_contaminated INTEGER DEFAULT 0,
  confidence TEXT DEFAULT 'medium',
  metadata_json TEXT,
  UNIQUE(wallet, mint, opened_at_utc),
  FOREIGN KEY(wallet) REFERENCES wallets(address),
  FOREIGN KEY(mint) REFERENCES tokens(mint)
);

-- Token-level cohorts: early buyers, top holders, winners, losers, sellers, dev-linked.
CREATE TABLE IF NOT EXISTS token_cohorts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mint TEXT NOT NULL,
  wallet TEXT NOT NULL,
  cohort_type TEXT NOT NULL, -- early_buyer, tracked_buyer, top_holder, first_seller, winner, loser, dev_related, sniper_cluster
  rank INTEGER,
  first_buy_utc TEXT,
  first_sell_utc TEXT,
  sol_spent REAL,
  sol_received REAL,
  amount_usd REAL,
  call_market_cap_usd REAL,
  realized_pnl_sol REAL,
  still_holding_tokens REAL,
  source_id TEXT,
  metadata_json TEXT,
  UNIQUE(mint, wallet, cohort_type),
  FOREIGN KEY(mint) REFERENCES tokens(mint),
  FOREIGN KEY(wallet) REFERENCES wallets(address),
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

-- Claims/signals: compatible with alpha_claim_ledger and secondary-export signals.
CREATE TABLE IF NOT EXISTS alpha_claims (
  claim_id TEXT PRIMARY KEY,
  source_id TEXT,
  source_type TEXT,
  asset TEXT,
  mint TEXT,
  wallet TEXT,
  ticker TEXT,
  claim_text TEXT,
  mechanism TEXT,
  evidence_class TEXT,
  primary_link TEXT,
  first_seen_utc TEXT,
  expected_effect TEXT,
  expiry_utc TEXT,
  lead_time_class TEXT, -- early, live, late, exit-liquidity, unknown
  confidence REAL,
  status TEXT DEFAULT 'open',
  outcome TEXT DEFAULT 'unknown',
  result_r REAL,
  error_type TEXT,
  metadata_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(mint) REFERENCES tokens(mint),
  FOREIGN KEY(wallet) REFERENCES wallets(address)
);

-- Historical signal examples from the secondary export.
CREATE TABLE IF NOT EXISTS token_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  source_signal_id TEXT,
  mint TEXT NOT NULL,
  signal_type TEXT,
  wallet_count INTEGER,
  tg_channel_count INTEGER,
  total_sol_amount REAL,
  call_market_cap_usd REAL,
  current_market_cap_usd REAL,
  ath_market_cap_usd REAL,
  ath_multiplier REAL,
  is_hit INTEGER,
  first_buy_utc TEXT,
  created_at_utc TEXT,
  captured_at_utc TEXT,
  score REAL,
  tier TEXT,
  metadata_json TEXT,
  UNIQUE(source_id, source_signal_id),
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(mint) REFERENCES tokens(mint)
);

-- Secondary concentration snapshots and future Helius holder snapshots.
CREATE TABLE IF NOT EXISTS token_concentration_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mint TEXT NOT NULL,
  total_value_usd REAL,
  supply_pct REAL,
  holder_count INTEGER,
  market_cap_usd REAL,
  snapshot_at_utc TEXT NOT NULL,
  source_id TEXT,
  run_id TEXT,
  metadata_json TEXT,
  FOREIGN KEY(mint) REFERENCES tokens(mint),
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(run_id) REFERENCES ingestion_runs(run_id)
);

CREATE TABLE IF NOT EXISTS wallet_scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wallet TEXT NOT NULL,
  score REAL,
  secondary_score REAL,
  actor_score REAL,
  classification TEXT,
  tier TEXT,
  confidence TEXT,
  pnl_all REAL,
  realized_pnl_sol REAL,
  win_rate REAL,
  avg_win_sol REAL,
  avg_loss_sol REAL,
  payoff_ratio REAL,
  median_hold_seconds INTEGER,
  buy_count INTEGER,
  sell_count INTEGER,
  airdrop_count INTEGER,
  sell_buy_ratio REAL,
  distinct_symbols INTEGER,
  trade_usd_sum REAL,
  transfer_contamination_score REAL,
  cluster_confidence REAL,
  copyability TEXT,
  reasons_json TEXT,
  source_id TEXT,
  scored_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(wallet) REFERENCES wallets(address),
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS source_scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  score REAL,
  total_calls INTEGER,
  wins INTEGER,
  losses INTEGER,
  undecided INTEGER,
  winrate_pct REAL,
  shrunk_winrate_pct REAL,
  avg_roi_pct REAL,
  current_streak INTEGER,
  tier TEXT,
  notes_json TEXT,
  scored_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

-- wallet_notes FTS lives in smart_wallets_notes_fts.sql; ensure_db() applies it only
-- when wallet_notes_fts is absent, because SQLite reserves the *_content name as a
-- shadow-table name once the FTS table exists and re-running the CREATE errors.

CREATE INDEX IF NOT EXISTS idx_wallet_edges_src ON wallet_edges(src_wallet);
CREATE INDEX IF NOT EXISTS idx_wallet_edges_dst ON wallet_edges(dst_wallet);
CREATE INDEX IF NOT EXISTS idx_wallet_edges_type ON wallet_edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_wallet_edges_mint ON wallet_edges(mint);
CREATE INDEX IF NOT EXISTS idx_events_wallet_time ON wallet_token_events(wallet, block_time_utc);
CREATE INDEX IF NOT EXISTS idx_events_mint_time ON wallet_token_events(mint, block_time_utc);
CREATE INDEX IF NOT EXISTS idx_events_signature ON wallet_token_events(signature);
CREATE INDEX IF NOT EXISTS idx_events_type ON wallet_token_events(event_type);
CREATE INDEX IF NOT EXISTS idx_positions_wallet_mint ON positions(wallet, mint);
CREATE INDEX IF NOT EXISTS idx_positions_pnl ON positions(realized_pnl_sol);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_cohorts_mint ON token_cohorts(mint);
CREATE INDEX IF NOT EXISTS idx_cohorts_wallet ON token_cohorts(wallet);
CREATE INDEX IF NOT EXISTS idx_scores_wallet_time ON wallet_scores(wallet, scored_at);
CREATE INDEX IF NOT EXISTS idx_token_signals_mint ON token_signals(mint);
CREATE INDEX IF NOT EXISTS idx_concentration_mint_time ON token_concentration_snapshots(mint, snapshot_at_utc);
