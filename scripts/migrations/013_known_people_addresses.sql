-- The harvester found the address and then threw it away, keeping only the
-- name. That defeats the point: an observed address is the strongest evidence
-- there is, and re-deriving it from a pattern turns a fact into a guess.
ALTER TABLE known_people ADD COLUMN email TEXT;
ALTER TABLE known_people ADD COLUMN email_observed_at TEXT;
ALTER TABLE known_people ADD COLUMN name_quality TEXT;   -- name | partial | handle
CREATE INDEX idx_known_people_email ON known_people(email);
