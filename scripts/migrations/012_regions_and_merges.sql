-- Region exclusions are a visible trade, not a silent drop. The GDPR rule holds
-- as a default, but the operator wants to see what it costs -- Mistral is
-- exactly the kind of target they would otherwise want -- so excluded companies
-- keep a distinct status and carry the evidence for the region call.
ALTER TABLE accounts ADD COLUMN region TEXT;
ALTER TABLE accounts ADD COLUMN region_source TEXT;

-- Acquisitions inside the roster. A snapshot of 667 companies will contain pairs
-- where one has since bought the other; Flock and Aerodome are the first found.
-- The acquired row is merged rather than deleted, so its people and its history
-- survive and neither row can be queued separately.
ALTER TABLE accounts ADD COLUMN merged_into_id INTEGER REFERENCES accounts(id);
ALTER TABLE accounts ADD COLUMN merged_at TEXT;
ALTER TABLE accounts ADD COLUMN merge_reason TEXT;
CREATE INDEX idx_accounts_merged_into ON accounts(merged_into_id);
