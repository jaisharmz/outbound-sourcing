-- Reachability of every linked document. A request-access wall is worse than no
-- link at all, so this is a gate rather than a warning.
CREATE TABLE link_checks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL,     -- ok | login_wall | permission_wall | dead
    detail      TEXT,
    fingerprint TEXT NOT NULL,     -- of the whole link set, so a change invalidates
    checked_at  TEXT NOT NULL
);
CREATE INDEX idx_link_checks_url ON link_checks(url);
