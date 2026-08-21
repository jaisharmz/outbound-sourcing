-- What the transitive personal-exclusion pass actually excluded, and why.
-- Recomputed rather than accumulated: the graph grows, so an exclusion that
-- holds today may not have been derivable yesterday, and the current answer is
-- the only one that matters. Kept as a table so it is reviewable -- an
-- exclusion nobody can see is indistinguishable from a bug that drops people.
CREATE TABLE exclusions_applied (
    id          INTEGER PRIMARY KEY,
    node_id     INTEGER REFERENCES graph_nodes(id),
    name        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    hops        INTEGER NOT NULL,   -- 0 = named directly, 1 = one hop out
    via         TEXT,               -- edge kind that transmitted it
    through     TEXT,               -- which named seed it came through
    source_url  TEXT,               -- the edge's evidence
    computed_at TEXT NOT NULL
);
CREATE INDEX idx_exclusions_applied_name ON exclusions_applied(name);
