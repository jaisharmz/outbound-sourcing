-- A domain's local-part convention, learned from observed addresses.
-- The addresses matter less than this does: one confirmed `first.last@` turns
-- every name found anywhere else into a candidate address.
ALTER TABLE accounts ADD COLUMN email_pattern TEXT;
ALTER TABLE accounts ADD COLUMN email_pattern_confidence REAL;
ALTER TABLE accounts ADD COLUMN email_pattern_evidence TEXT;
