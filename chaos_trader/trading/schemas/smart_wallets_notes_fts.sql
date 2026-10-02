-- wallet_notes external-content FTS5 pair. Applied by smart_wallet_tracker.ensure_db()
-- ONLY when wallet_notes_fts is absent: once the FTS table exists, SQLite reserves the
-- wallet_notes_fts_content name as a shadow-table name and re-running this script errors.

CREATE TABLE IF NOT EXISTS wallet_notes_fts_content (
  rowid INTEGER PRIMARY KEY,
  wallet TEXT,
  note TEXT,
  source TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS wallet_notes_fts USING fts5(
  wallet,
  note,
  source,
  created_at,
  content='wallet_notes_fts_content',
  content_rowid='rowid'
);
