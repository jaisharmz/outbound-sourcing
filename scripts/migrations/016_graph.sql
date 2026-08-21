-- A persistent people-graph. The pipeline was company -> people, which is a
-- lookup; this makes company an attribute of a node and the search a traversal.
-- It accumulates across runs, so every run makes the next one cheaper.

CREATE TABLE graph_nodes (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,          -- person | organization | lab | paper
    key           TEXT NOT NULL,          -- canonical: openalex id, or normalized name
    display_name  TEXT NOT NULL,
    external_ids  TEXT,                   -- json: openalex, orcid, s2, github, homepage
    attrs         TEXT,                   -- json: seniority, topics, works_count...
    first_seen_run TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (kind, key)
);
CREATE INDEX idx_graph_nodes_kind ON graph_nodes(kind);
CREATE INDEX idx_graph_nodes_name ON graph_nodes(display_name);

-- Every edge carries a source URL. Same evidence contract as a candidate record:
-- an unsourced relationship is an assertion, and assertions do not get emailed.
CREATE TABLE graph_edges (
    id            INTEGER PRIMARY KEY,
    src_id        INTEGER NOT NULL REFERENCES graph_nodes(id),
    dst_id        INTEGER NOT NULL REFERENCES graph_nodes(id),
    kind          TEXT NOT NULL,   -- coauthored_with | advised_by | advises
                                   -- | lab_member_of | works_at
    year          INTEGER,
    is_current    INTEGER,         -- works_at: current vs former
    paper_key     TEXT,
    source_url    TEXT NOT NULL,
    quote         TEXT,
    retrieved_at  TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (src_id, dst_id, kind, paper_key, year)
);
CREATE INDEX idx_graph_edges_src ON graph_edges(src_id, kind);
CREATE INDEX idx_graph_edges_dst ON graph_edges(dst_id, kind);

-- How a node was reached. Someone reachable independently by three routes is
-- more central than someone found once, and that is a ranking signal rather
-- than a duplicate to collapse.
CREATE TABLE graph_paths (
    id            INTEGER PRIMARY KEY,
    node_id       INTEGER NOT NULL REFERENCES graph_nodes(id),
    run_id        TEXT NOT NULL,
    seed_node_id  INTEGER REFERENCES graph_nodes(id),
    hops          INTEGER NOT NULL,
    via           TEXT,            -- edge kinds traversed, joined
    created_at    TEXT NOT NULL,
    UNIQUE (node_id, run_id, seed_node_id, via)
);
CREATE INDEX idx_graph_paths_node ON graph_paths(node_id);

-- Expansion decisions, so a traversal's reasoning is reviewable rather than
-- only its output.
CREATE TABLE graph_expansions (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL,
    node_id       INTEGER REFERENCES graph_nodes(id),
    decision      TEXT NOT NULL,   -- expanded | skipped
    reason        TEXT NOT NULL,
    yielded       INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_graph_expansions_run ON graph_expansions(run_id);
