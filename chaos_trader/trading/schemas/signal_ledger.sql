-- Chaos signal ledger dimensions added by profile-as-code repo.
-- Existing live DB migrations are performed defensively by signal_ledger.connect().

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT UNIQUE NOT NULL,
    timestamp_utc TEXT NOT NULL,
    source_command TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    verdict TEXT,
    signal_kind TEXT,
    entry_action TEXT,
    structural_gate TEXT,
    legacy_verdict TEXT,
    position_action TEXT,
    catalyst_type TEXT,
    flow_conversion_status TEXT,
    fake_flow_severity TEXT,
    attention_phase TEXT,
    score REAL,
    fact_grade TEXT,
    raw_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
