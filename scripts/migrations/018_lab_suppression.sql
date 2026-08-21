-- Traversal makes it easy to email four people from one research group without
-- noticing, and they talk to each other. Lab-level suppression sits alongside
-- company-level: a cap per lab, and one reply suppresses the whole lab.
ALTER TABLE contacts ADD COLUMN lab TEXT;
CREATE INDEX idx_contacts_lab ON contacts(lab);

-- Who was excluded because the operator already knows them, transitively, and
-- how far out the boundary was drawn. Recorded so the boundary is reviewable
-- rather than invisible.
CREATE TABLE exclusion_log (
    id            INTEGER PRIMARY KEY,
    person        TEXT NOT NULL,
    lab           TEXT,
    rule          TEXT NOT NULL,     -- lab_member | lab_collaborator | named_person
    hops          INTEGER,
    detail        TEXT,
    created_at    TEXT NOT NULL
);
