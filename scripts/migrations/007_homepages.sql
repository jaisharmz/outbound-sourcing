-- A company's own words, which are better evidence than its investor's one-line
-- blurb. Stored for every company, not only the ones missing a blurb, so stage 0
-- can be re-judged later against the better text without re-fetching anything.

ALTER TABLE accounts ADD COLUMN homepage_url TEXT;
ALTER TABLE accounts ADD COLUMN homepage_text TEXT;
ALTER TABLE accounts ADD COLUMN homepage_fetched_at TEXT;
-- Why a homepage produced no text, kept distinct from "produced text with no AI
-- signal". A site that did not fetch is unknown, never fail.
--   ok        meaningful text extracted
--   js_shell  HTML returned, but the content is client-rendered
--   holding   a parked or coming-soon page
--   dead      DNS failure, connection refused, or 4xx/5xx
--   blocked   bot challenge or explicit refusal
ALTER TABLE accounts ADD COLUMN homepage_fetch_status TEXT;
CREATE INDEX idx_accounts_homepage_status ON accounts(homepage_fetch_status);
