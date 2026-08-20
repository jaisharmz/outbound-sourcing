-- --to reaches an address that never passed the review gate, so record how each
-- test send was authorized rather than only that it happened.
ALTER TABLE test_sends ADD COLUMN allowlisted INTEGER NOT NULL DEFAULT 1;
ALTER TABLE test_sends ADD COLUMN forced INTEGER NOT NULL DEFAULT 0;
