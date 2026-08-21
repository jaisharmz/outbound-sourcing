-- Fund portfolios and the stage-0 pre-filter.

-- Pre-filter verdict, kept recoverable on purpose. The description the verdict
-- was computed from is stored, and the raw fund payload is cached, so stage 0
-- can be re-run with better rules without re-fetching anything.
ALTER TABLE accounts ADD COLUMN prefilter TEXT;              -- pass | fail | unknown
ALTER TABLE accounts ADD COLUMN prefilter_rule TEXT;         -- which ruleset decided
ALTER TABLE accounts ADD COLUMN prefilter_evidence TEXT;     -- the text it judged
ALTER TABLE accounts ADD COLUMN prefilter_at TEXT;
ALTER TABLE accounts ADD COLUMN fund TEXT;
ALTER TABLE accounts ADD COLUMN stages TEXT;                 -- note only, never a filter
ALTER TABLE accounts ADD COLUMN verticals TEXT;              -- note only, never a filter
ALTER TABLE accounts ADD COLUMN year_founded TEXT;
CREATE INDEX idx_accounts_prefilter ON accounts(prefilter);

-- People known before discovery runs. A fund portfolio names founders at zero
-- search cost, which is a third of the a16z roster. Separate from `contacts`
-- because these have no email yet, and `contacts` means "has an address".
CREATE TABLE known_people (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    name          TEXT NOT NULL,
    role          TEXT,
    provenance    TEXT NOT NULL,      -- fund_portfolio | industry_run | manual
    source_url    TEXT,
    resolved      INTEGER NOT NULL DEFAULT 0,   -- set when a contact row exists
    created_at    TEXT NOT NULL,
    UNIQUE (account_id, name)
);
CREATE INDEX idx_known_people_account ON known_people(account_id);
