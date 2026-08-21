-- Repairs migration 017, which rebuilt `contacts` with CREATE TABLE ... AS
-- SELECT. That copies rows and types but drops the primary key, so
-- evidence.contact_id had nothing to reference and every foreign key touching
-- contacts failed with "foreign key mismatch".
--
-- Rebuilt properly here, with the CHECK on verification_status widened to admit
-- mx_only, which is what 017 was trying to achieve.

PRAGMA foreign_keys = OFF;

CREATE TABLE contacts_rebuilt (
    id                          INTEGER PRIMARY KEY,
    account_id                  INTEGER NOT NULL REFERENCES accounts(id),
    name                        TEXT NOT NULL,
    first_name                  TEXT NOT NULL,
    last_name                   TEXT,
    title                       TEXT NOT NULL,
    email                       TEXT NOT NULL UNIQUE,
    email_domain                TEXT NOT NULL,
    email_basis                 TEXT NOT NULL,
    confidence                  REAL NOT NULL,
    personalization             TEXT,
    personalization_source_url  TEXT,
    country                     TEXT,
    linkedin_url                TEXT,
    verification_status         TEXT NOT NULL DEFAULT 'unverified',
    verification_detail         TEXT,
    verified_at                 TEXT,
    approved                    INTEGER NOT NULL DEFAULT 0,
    approved_at                 TEXT,
    status                      TEXT NOT NULL DEFAULT 'new',
    stopped_reason              TEXT,
    candidate_file              TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    tier                        TEXT,
    campaign                    TEXT,
    ai_depth                    TEXT,
    sendable                    INTEGER NOT NULL DEFAULT 1,
    unsendable_reason           TEXT,
    lab                         TEXT,
    CHECK (email_basis IN ('observed','inferred_from_pattern')),
    CHECK (verification_status IN
           ('unverified','valid','catch_all','mx_only','invalid','unknown'))
);

INSERT INTO contacts_rebuilt SELECT
    id, account_id, name, first_name, last_name, title, email, email_domain,
    email_basis, confidence, personalization, personalization_source_url,
    country, linkedin_url, verification_status, verification_detail, verified_at,
    approved, approved_at, status, stopped_reason, candidate_file, created_at,
    updated_at, tier, campaign, ai_depth, sendable, unsendable_reason, lab
  FROM contacts;

DROP TABLE contacts;
ALTER TABLE contacts_rebuilt RENAME TO contacts;

CREATE INDEX idx_contacts_account ON contacts(account_id);
CREATE INDEX idx_contacts_status ON contacts(status);
CREATE INDEX idx_contacts_verification ON contacts(verification_status);
CREATE INDEX idx_contacts_tier ON contacts(tier);
CREATE INDEX idx_contacts_lab ON contacts(lab);

PRAGMA foreign_keys = ON;
