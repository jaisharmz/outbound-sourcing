-- Company discovery. Tier is carried all the way to the contact so reporting
-- can separate reply rate by tier and by campaign: two segments that fail for
-- different reasons produce a blended number that hides which one works.

ALTER TABLE accounts ADD COLUMN tier TEXT;
ALTER TABLE accounts ADD COLUMN campaign TEXT;
ALTER TABLE accounts ADD COLUMN what TEXT;
ALTER TABLE accounts ADD COLUMN entry_note TEXT;
ALTER TABLE accounts ADD COLUMN ships INTEGER;
ALTER TABLE accounts ADD COLUMN subproblems TEXT;
ALTER TABLE accounts ADD COLUMN evidence_url TEXT;
-- A landscape `url` is an evidence link, not a homepage: Google DeepMind's is
-- an arXiv abstract. Treat a domain taken from it as a candidate, never a fact.
ALTER TABLE accounts ADD COLUMN domain_confidence TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE contacts ADD COLUMN tier TEXT;
ALTER TABLE contacts ADD COLUMN campaign TEXT;

CREATE INDEX idx_accounts_tier ON accounts(tier);
CREATE INDEX idx_accounts_campaign ON accounts(campaign);
CREATE INDEX idx_contacts_tier ON contacts(tier);
