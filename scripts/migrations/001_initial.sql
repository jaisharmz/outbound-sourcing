-- Single source of truth. Agentic discovery writes JSON; ingest writes here;
-- nothing downstream reads anything else.

CREATE TABLE accounts (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    name_normalized   TEXT NOT NULL UNIQUE,
    domain            TEXT,
    source            TEXT NOT NULL,           -- list | vc | industry
    source_ref        TEXT,                    -- run dir, list path, avenue slug
    country           TEXT,
    status            TEXT NOT NULL DEFAULT 'new',   -- new | researching | done | degraded | excluded
    excluded_reason   TEXT,
    searches_used     INTEGER NOT NULL DEFAULT 0,
    budget_exhausted  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX idx_accounts_status ON accounts(status);
CREATE INDEX idx_accounts_domain ON accounts(domain);

CREATE TABLE contacts (
    id                          INTEGER PRIMARY KEY,
    account_id                  INTEGER NOT NULL REFERENCES accounts(id),
    name                        TEXT NOT NULL,
    first_name                  TEXT NOT NULL,
    last_name                   TEXT,
    title                       TEXT NOT NULL,
    email                       TEXT NOT NULL UNIQUE,
    email_domain                TEXT NOT NULL,
    email_basis                 TEXT NOT NULL,   -- observed | inferred_from_pattern
    confidence                  REAL NOT NULL,
    personalization             TEXT,
    personalization_source_url  TEXT,
    timezone                    TEXT,
    country                     TEXT,
    linkedin_url                TEXT,
    verification_status         TEXT NOT NULL DEFAULT 'unverified',
                                -- unverified | valid | catch_all | invalid | unknown
    verification_detail         TEXT,
    verified_at                 TEXT,
    approved                    INTEGER NOT NULL DEFAULT 0,
    approved_at                 TEXT,
    status                      TEXT NOT NULL DEFAULT 'new',
                                -- new | verified | approved | queued | active | stopped | dropped
    stopped_reason              TEXT,
    candidate_file              TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    CHECK (email_basis IN ('observed','inferred_from_pattern')),
    CHECK (verification_status IN ('unverified','valid','catch_all','invalid','unknown'))
);
CREATE INDEX idx_contacts_account ON contacts(account_id);
CREATE INDEX idx_contacts_status ON contacts(status);
CREATE INDEX idx_contacts_verification ON contacts(verification_status);

-- Every claim that reached the database keeps its URL. This table is what makes
-- the review gate reviewable.
CREATE TABLE evidence (
    id            INTEGER PRIMARY KEY,
    contact_id    INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    claim         TEXT NOT NULL,
    url           TEXT NOT NULL,
    quote         TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL
);
CREATE INDEX idx_evidence_contact ON evidence(contact_id);

CREATE TABLE campaigns (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE enrollments (
    id              INTEGER PRIMARY KEY,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    current_step    TEXT,
    next_step       TEXT,
    next_send_at    TEXT,
    stopped         INTEGER NOT NULL DEFAULT 0,
    stopped_reason  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (contact_id, campaign_id)
);
CREATE INDEX idx_enrollments_due ON enrollments(stopped, next_send_at);

-- Crash safety lives here. A row moves to 'sending' and commits BEFORE the
-- provider call, then to 'sent' after. A crash between the two leaves a
-- 'sending' row, which reconciliation resolves against provider state rather
-- than by blindly retrying.
CREATE TABLE messages (
    id                   INTEGER PRIMARY KEY,
    contact_id           INTEGER NOT NULL REFERENCES contacts(id),
    campaign_id          INTEGER NOT NULL REFERENCES campaigns(id),
    step_id              TEXT NOT NULL,
    mailbox_id           TEXT NOT NULL,
    state                TEXT NOT NULL DEFAULT 'queued',
                         -- queued | sending | sent | failed | skipped | cancelled
    to_addr              TEXT NOT NULL,
    cc                   TEXT NOT NULL DEFAULT '',
    bcc                  TEXT NOT NULL DEFAULT '',
    recipient_count      INTEGER NOT NULL DEFAULT 1,
    subject              TEXT NOT NULL,
    body_hash            TEXT NOT NULL,
    template_hash        TEXT NOT NULL,
    variant              TEXT,               -- attachments | links, for the A/B
    attachment_names     TEXT NOT NULL DEFAULT '',
    idempotency_key      TEXT NOT NULL UNIQUE,
    provider_message_id  TEXT,
    provider_thread_id   TEXT,
    queued_at            TEXT,
    sending_at           TEXT,
    sent_at              TEXT,
    failed_at            TEXT,
    error                TEXT,
    CHECK (state IN ('queued','sending','sent','failed','skipped','cancelled'))
);
CREATE INDEX idx_messages_state ON messages(state);
CREATE INDEX idx_messages_mailbox_sent ON messages(mailbox_id, sent_at);
CREATE INDEX idx_messages_thread ON messages(provider_thread_id);

CREATE TABLE replies (
    id              INTEGER PRIMARY KEY,
    message_id      INTEGER REFERENCES messages(id),
    contact_id      INTEGER REFERENCES contacts(id),
    thread_id       TEXT,
    provider_id     TEXT UNIQUE,
    received_at     TEXT NOT NULL,
    classification  TEXT,
                    -- interested | not_interested | referral | ooo | unsubscribe | bounce
    classified_by   TEXT,   -- rules | model | human
    body_excerpt    TEXT,
    handled         INTEGER NOT NULL DEFAULT 0,
    draft_id        TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_replies_thread ON replies(thread_id);

-- Permanent, global, checked at every stage including discovery.
CREATE TABLE suppression (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,     -- email | domain | company
    value       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    source      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (kind, value)
);
CREATE INDEX idx_suppression_value ON suppression(value);

-- The scheduler refuses to send from a mailbox with no passing test send, and
-- refuses to start a campaign whose templates changed since the last one.
CREATE TABLE test_sends (
    id             INTEGER PRIMARY KEY,
    mailbox_id     TEXT NOT NULL,
    step_id        TEXT NOT NULL,
    campaign       TEXT,
    template_hash  TEXT NOT NULL,
    to_addr        TEXT NOT NULL,
    ok             INTEGER NOT NULL DEFAULT 0,
    headers        TEXT,
    error          TEXT,
    sent_at        TEXT NOT NULL
);
CREATE INDEX idx_test_sends_mailbox ON test_sends(mailbox_id, sent_at);

-- Cap accounting counts recipients, not messages: a CC consumes provider
-- recipient quota. See references/deliverability.md for why that is not the
-- binding constraint in practice.
CREATE TABLE mailbox_day (
    mailbox_id  TEXT NOT NULL,
    day         TEXT NOT NULL,
    messages    INTEGER NOT NULL DEFAULT 0,
    recipients  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mailbox_id, day)
);

CREATE TABLE circuit_breaker (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    tripped     INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    tripped_at  TEXT,
    resumed_at  TEXT
);
INSERT INTO circuit_breaker (id, tripped) VALUES (1, 0);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL,
    event       TEXT NOT NULL,
    payload     TEXT
);
CREATE INDEX idx_events_ts ON events(ts);
