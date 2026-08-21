-- Independent recency check on an account. GitHub activity proves engineers are
-- working; it does not prove the company still exists independently. Anyscale
-- had a commit from yesterday and had been acquired three weeks earlier.
ALTER TABLE accounts ADD COLUMN liveness_checked_at TEXT;
ALTER TABLE accounts ADD COLUMN liveness_status TEXT;    -- live | acquired | shutdown | ambiguous
ALTER TABLE accounts ADD COLUMN liveness_note TEXT;
ALTER TABLE accounts ADD COLUMN liveness_source TEXT;
