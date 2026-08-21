-- How deep a company's AI work goes, kept separate from tier.
--
--   builds   trains or fine-tunes its own models, has research staff, publishes
--   applies  ships AI features on top of someone else's models
--
-- Different pitch and different campaign. Blended into one verdict the reply
-- rates cannot be read apart, which is the same reason tier is carried through.
ALTER TABLE accounts ADD COLUMN ai_depth TEXT;   -- builds | applies
ALTER TABLE contacts ADD COLUMN ai_depth TEXT;
CREATE INDEX idx_accounts_ai_depth ON accounts(ai_depth);
