-- The a16z import is a fallback pool, not the working set. It was imported when
-- the target was 500/day and nobody chose those 88 companies; the working set is
-- whatever the operator names or picks from an industry suggestion.
ALTER TABLE accounts ADD COLUMN pool TEXT NOT NULL DEFAULT 'working';  -- working | fallback
CREATE INDEX idx_accounts_pool ON accounts(pool);

-- Companies used to exercise the pipeline are not campaign inventory. Their
-- contacts must never queue, whatever else happens to them.
ALTER TABLE accounts ADD COLUMN validation_run INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts ADD COLUMN sendable INTEGER NOT NULL DEFAULT 1;
ALTER TABLE contacts ADD COLUMN unsendable_reason TEXT;
