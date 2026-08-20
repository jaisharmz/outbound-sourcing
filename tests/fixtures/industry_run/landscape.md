# Landscape

Prose the adapter must not parse.

```yaml
inclusion_test: Does the organization ship the thing this field is about?
excluded:
  - name: Excluded Co
    why: Sells a different thing entirely.
    evidence: https://example.test/why
orgs:
  - name: Homepage Startup
    tier: startup
    url: https://homepagestartup.test/
    what: Ships the thing.
    subproblems: [alpha, beta]
    ships: true
    entry: Open source contributions.
    evidence: https://homepagestartup.test/blog
  - name: Arxiv Lab
    tier: frontier-lab
    url: https://arxiv.org/abs/2501.00663
    what: Publishes a lot.
    subproblems: [alpha]
    ships: true
    entry: Research residency.
    evidence: https://arxiv.org/abs/2501.00663
  - name: Docs Company
    tier: startup
    url: https://docs.google.com/document/d/abc
    what: Cited via a shared doc.
    ships: false
    entry: Unknown.
    evidence: https://docs.google.com/document/d/abc
  - name: Personal Site Lab
    tier: academic
    url: https://someprofessor.test/
    what: A university group.
    ships: false
    entry: PhD hiring.
    evidence: https://someprofessor.test/
  - name: Funded Startup
    tier: startup
    url: https://fundedstartup.test/product
    what: Ships a product.
    stage: series-a
    raised: $20M
    investors: [Some Fund]
    headcount: 30
    ships: true
    entry: Standard hiring.
    evidence: https://fundedstartup.test/product
investors: []
lineage: []
timeline: []
```
