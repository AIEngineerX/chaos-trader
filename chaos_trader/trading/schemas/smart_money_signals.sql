-- Archive of pulled smart-money cluster signals (external signal API feed).
-- Runtime DB lives under trading/db/; never a Git/package input.
-- Rows are also fed into the signal ledger (source_command="smart-signals") so their
-- realized outcomes are measured by signal_calibration_report.py alongside every other
-- verdict source.

CREATE TABLE IF NOT EXISTS smart_money_signals (
    signal_id        TEXT PRIMARY KEY,
    pulled_at        TEXT NOT NULL,
    token            TEXT NOT NULL,
    symbol           TEXT,
    distinct_wallets INTEGER,
    weighted_score   INTEGER,
    tier_a           INTEGER,
    tier_b           INTEGER,
    tier_c           INTEGER,
    call_market_cap  REAL,
    raw_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sms_token ON smart_money_signals (token);
