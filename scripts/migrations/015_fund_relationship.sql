-- Some funds carry a warm-intro route. A company reached through a fellowship
-- connection should not receive a cold email; the right move there is an ask,
-- not a sequence.
ALTER TABLE accounts ADD COLUMN relationship TEXT;   -- e.g. fellowship
CREATE INDEX idx_accounts_relationship ON accounts(relationship);
