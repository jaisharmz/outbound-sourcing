"""Graph store. Evidence contract, dedup, path counting and scoring."""

from __future__ import annotations

import pytest

from scripts import graph as G


def test_an_edge_without_a_source_is_refused(conn):
    """An unsourced relationship is an assertion, and assertions do not get
    emailed. Same contract as a candidate record."""
    a = G.upsert_node(conn, "person", "Ada Lovelace")
    b = G.upsert_node(conn, "person", "Grace Hopper")
    with pytest.raises(G.GraphError, match="absolute source URL"):
        G.add_edge(conn, a, b, "coauthored_with", source_url="")
    with pytest.raises(G.GraphError, match="absolute source URL"):
        G.add_edge(conn, a, b, "coauthored_with", source_url="openalex.org/W1")


def test_unknown_kinds_are_refused(conn):
    with pytest.raises(G.GraphError, match="unknown node kind"):
        G.upsert_node(conn, "planet", "Mars")
    a = G.upsert_node(conn, "person", "A Person")
    b = G.upsert_node(conn, "person", "B Person")
    with pytest.raises(G.GraphError, match="unknown edge kind"):
        G.add_edge(conn, a, b, "friends_with", source_url="https://x.test/1")


def test_a_node_is_deduplicated_by_external_id(conn):
    a = G.upsert_node(conn, "person", "Ren Kovic", external={"openalex": "A123"})
    b = G.upsert_node(conn, "person", "T. Dao", external={"openalex": "A123"})
    assert a == b


def test_attrs_merge_rather_than_overwrite(conn):
    import json
    a = G.upsert_node(conn, "person", "Ada Lovelace", external={"openalex": "A1"},
                      attrs={"works": 5})
    G.upsert_node(conn, "person", "Ada Lovelace", external={"openalex": "A1", "orcid": "0000"},
                  attrs={"cited": 9})
    row = conn.execute("SELECT external_ids, attrs FROM graph_nodes WHERE id=?", (a,)).fetchone()
    assert json.loads(row["external_ids"]) == {"openalex": "A1", "orcid": "0000"}
    assert json.loads(row["attrs"]) == {"works": 5, "cited": 9}


def test_path_count_measures_independent_routes(conn):
    """Reachable by three routes is more central than found once, and that is a
    ranking signal rather than a duplicate to collapse."""
    n = G.upsert_node(conn, "person", "Target")
    s1 = G.upsert_node(conn, "person", "Seed One")
    s2 = G.upsert_node(conn, "person", "Seed Two")
    G.record_path(conn, n, "r1", seed_node_id=s1, hops=1, via="seed>coauthored_with")
    G.record_path(conn, n, "r1", seed_node_id=s1, hops=1, via="seed>coauthored_with")
    assert G.path_count(conn, n) == 1
    G.record_path(conn, n, "r1", seed_node_id=s2, hops=2, via="seed>advises>coauthored_with")
    assert G.path_count(conn, n) == 2


def test_self_edges_are_dropped(conn):
    a = G.upsert_node(conn, "person", "Solo")
    assert G.add_edge(conn, a, a, "coauthored_with", source_url="https://x.test/1") is False


# ---------------------------------------------------------------- scoring


def test_hubs_are_penalised(conn):
    """A PI with two hundred coauthors reaches everyone and distinguishes no one."""
    hub = G.upsert_node(conn, "person", "Hub PI")
    for i in range(90):
        other = G.upsert_node(conn, "person", f"Person {i}")
        G.add_edge(conn, hub, other, "coauthored_with", source_url=f"https://x.test/{i}")
    lone = G.upsert_node(conn, "person", "Quiet Researcher")
    G.record_path(conn, hub, "r", seed_node_id=None, hops=1, via="seed")
    G.record_path(conn, lone, "r", seed_node_id=None, hops=1, via="seed")
    h = G.score_node(conn, hub, hops=1, seniority="research_scientist")
    q = G.score_node(conn, lone, hops=1, seniority="research_scientist")
    assert q.total > h.total
    assert any("hub penalty" in n for n in h.notes)


def test_mid_career_outranks_both_ends(conn):
    n = G.upsert_node(conn, "person", "X")
    G.record_path(conn, n, "r", seed_node_id=None, hops=1, via="seed")
    mid = G.score_node(conn, n, hops=1, seniority="research_scientist").total
    pi = G.score_node(conn, n, hops=1, seniority="pi").total
    first = G.score_node(conn, n, hops=1, seniority="first_year").total
    assert mid > pi and mid > first


def test_recency_and_corroboration_move_the_score(conn):
    n = G.upsert_node(conn, "person", "X")
    G.record_path(conn, n, "r", seed_node_id=None, hops=1, via="a")
    old = G.score_node(conn, n, hops=1, latest_year=2015).total
    new = G.score_node(conn, n, hops=1, latest_year=2026).total
    assert new > old
    for i in range(3):
        seed = G.upsert_node(conn, "person", f"Seed {i}")
        G.record_path(conn, n, "r", seed_node_id=seed, hops=1, via=f"route{i}")
    assert G.score_node(conn, n, hops=1, latest_year=2026).total > new


def test_score_explains_itself(conn):
    n = G.upsert_node(conn, "person", "X")
    G.record_path(conn, n, "r", seed_node_id=None, hops=2, via="a")
    s = G.score_node(conn, n, hops=2, topic_match=0.9, latest_year=2025,
                     email_resolvable=True, seniority="postdoc")
    assert set(s.parts) == {"proximity", "corroboration", "topic", "recency",
                            "reachable", "seniority"}
    assert 0 <= s.total <= 1


def test_expansion_decisions_are_recorded(conn):
    n = G.upsert_node(conn, "person", "Some PI")
    G.log_expansion(conn, "r1", n, "skipped", "PI band; expanding reaches everyone")
    row = conn.execute("SELECT decision, reason FROM graph_expansions").fetchone()
    assert row["decision"] == "skipped" and "PI band" in row["reason"]


def test_a_name_only_node_is_adopted_when_an_id_arrives(conn):
    """Met by name from a lab page, met again by OpenAlex id from a paper."""
    first = G.upsert_node(conn, "person", "Ada Lovelace")
    second = G.upsert_node(conn, "person", "Ada Lovelace", external={"openalex": "A99"})
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE kind='person'"
                        ).fetchone()[0] == 1


