-- Outbound port 25 is blocked on most residential and cloud networks, so an
-- SMTP RCPT probe cannot run and every address returns `unknown`. That is a
-- fact about the network, not about the address, and conflating the two would
-- either block every send or hide a real failure.
--
-- `mx_only` means the domain publishes MX and accepts mail while the mailbox
-- itself was not probed. At 20 sends a day behind a human review gate that is an
-- acceptable basis, and the review export flags it as one.
PRAGMA foreign_keys = OFF;

CREATE TABLE contacts_new AS SELECT * FROM contacts;
DROP TABLE contacts;
ALTER TABLE contacts_new RENAME TO contacts;

CREATE UNIQUE INDEX idx_contacts_email ON contacts(email);
CREATE INDEX idx_contacts_account ON contacts(account_id);
CREATE INDEX idx_contacts_status ON contacts(status);
CREATE INDEX idx_contacts_verification ON contacts(verification_status);
CREATE INDEX idx_contacts_tier ON contacts(tier);

PRAGMA foreign_keys = ON;
