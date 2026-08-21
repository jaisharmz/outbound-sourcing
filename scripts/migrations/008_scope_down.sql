-- Scope-down to a single mailbox at 15-25 sends/day, invoked by hand.
--
-- Removed here rather than left dormant, because a column nobody writes is a
-- column somebody later reads and believes:
--   messages.variant       the links-vs-attachments A/B is gone; attachments and
--                          links now coexist in one email
--   contacts.timezone      recipient-local sending windows are gone
--   circuit_breaker        the trailing-bounce halt protected a dedicated
--                          sending domain that no longer exists
--
-- Bounce detection stays. Address quality is independent of volume, and bounces
-- feed the permanent suppression list.

ALTER TABLE messages DROP COLUMN variant;
ALTER TABLE contacts DROP COLUMN timezone;
DROP TABLE circuit_breaker;

-- Inbound mail that matched no tracked thread. Reply matching over IMAP works by
-- finding our Message-ID in In-Reply-To/References, and when it fails it fails
-- silently -- which is worse than failing loudly, because stop-on-reply is what
-- prevents emailing someone who already answered.
CREATE TABLE unmatched_inbound (
    id            INTEGER PRIMARY KEY,
    provider_id   TEXT UNIQUE,
    from_addr     TEXT,
    subject       TEXT,
    received_at   TEXT,
    in_reply_to   TEXT,
    references_hdr TEXT,
    reviewed      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_unmatched_reviewed ON unmatched_inbound(reviewed);
