-- Sample count behind a learned pattern. first.last at 1.00 from three
-- addresses and first.last at 1.00 from one are very different claims, and with
-- catch_all sending freely the review gate is the only check -- so the count
-- travels with the pattern into the review export.
ALTER TABLE accounts ADD COLUMN email_pattern_samples INTEGER;

-- Recency signals from the harvest. Staleness catches people who left; it does
-- not catch companies that were acquired or shut down. Anyscale's founders had
-- recent commits and the company had stopped existing three weeks earlier.
-- These let a fresh-commits-versus-stale-record tension be surfaced rather than
-- either side being trusted.
ALTER TABLE accounts ADD COLUMN newest_commit_at TEXT;
ALTER TABLE accounts ADD COLUMN github_org TEXT;
ALTER TABLE accounts ADD COLUMN github_archived_repos INTEGER;
