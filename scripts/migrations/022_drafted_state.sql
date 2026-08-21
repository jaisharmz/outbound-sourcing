-- A draft is not a send. Reply tracking, suppression, the daily cap and every
-- "have we contacted this person" question must key off the message actually
-- leaving, so a draft the operator never sends must not count as contact.
--
-- SQLite cannot alter a CHECK constraint in place, so messages is rebuilt. The
-- rebuild is written out column by column rather than with CREATE TABLE AS
-- SELECT, which silently drops the primary key -- that was migration 017's bug
-- and it took a second migration to undo.
-- The migration runner supplies BEGIN/COMMIT; a second pair here fails
-- with "cannot start a transaction within a transaction".

CREATE TABLE messages_new (
    id                   INTEGER PRIMARY KEY,
    contact_id           INTEGER NOT NULL REFERENCES contacts(id),
    campaign_id          INTEGER,
    step_id              TEXT NOT NULL,
    mailbox_id           TEXT NOT NULL,
    state                TEXT NOT NULL DEFAULT 'queued',
    to_addr              TEXT NOT NULL,
    cc                   TEXT,
    bcc                  TEXT,
    recipient_count      INTEGER NOT NULL DEFAULT 1,
    subject              TEXT,
    body_hash            TEXT,
    template_hash        TEXT,
    attachment_names     TEXT,
    idempotency_key      TEXT UNIQUE,
    provider_message_id  TEXT,
    provider_thread_id   TEXT,
    error                TEXT,
    queued_at            TEXT,
    sending_at           TEXT,
    sent_at              TEXT,
    failed_at            TEXT,
    drafted_at           TEXT,
    CHECK (state IN ('queued','sending','sent','failed','skipped','cancelled',
                     'drafting','drafted'))
);

INSERT INTO messages_new (id, contact_id, campaign_id, step_id, mailbox_id, state,
    to_addr, cc, bcc, recipient_count, subject, body_hash, template_hash,
    attachment_names, idempotency_key, provider_message_id, provider_thread_id,
    error, queued_at, sending_at, sent_at, failed_at)
SELECT id, contact_id, campaign_id, step_id, mailbox_id, state,
    to_addr, cc, bcc, recipient_count, subject, body_hash, template_hash,
    attachment_names, idempotency_key, provider_message_id, provider_thread_id,
    error, queued_at, sending_at, sent_at, failed_at
FROM messages;

DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;
CREATE INDEX idx_messages_state ON messages(state);
CREATE INDEX idx_messages_contact ON messages(contact_id);

