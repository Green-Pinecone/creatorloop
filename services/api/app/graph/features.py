"""Graph analysis: linkage clusters (WCC), engagement communities (Louvain), TrustRank (personalized
PageRank) and clone detection (Jaccard). Pure functions over a GraphStore, so they are unit-testable."""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

import networkx as nx

from .store import GraphStore

WEI = 10**18


@dataclass
class Feat:
    linkage_cluster_id: int = -1
    linkage_cluster_size: int = 1
    community_id: int = -1
    community_size: int = 1
    community_internal_ratio: float = 0.0
    community_reciprocity: float = 0.0
    trustrank: float = 0.0
    trustrank_norm: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    feats: dict[str, Feat]
    clusters: list[dict]  # {members, signals}
    communities: list[dict]  # {members, internal_ratio, reciprocity}
    clone_groups: list[set[str]]
    clone_pairs: list[tuple[str, str, int, float]]
    engagement: dict[tuple[str, str], float]
    has_seeds: bool


def build_linkage(store: GraphStore, max_shared_degree: int = 25) -> dict[tuple[str, str], set[str]]:
    """Accounts sharing a device / network / payer / funder. Shared nodes with too many members (venue Wi-Fi,
    CGNAT, public faucet funders) are supernodes: they link everyone and mean nothing, so they are ignored."""
    shared: list[tuple[str, set[str]]] = []
    for kind, nodes in store.signals.items():
        shared += [(f"{kind}:{n}", m) for n, m in nodes.items()]
    by_funder: dict[str, set[str]] = defaultdict(set)
    for wallet, owner in store.wallet_owner.items():
        if funder := store.funded_by.get(wallet):
            by_funder[funder].add(owner)
    shared += [(f"Funder:{f}", m) for f, m in by_funder.items()]
    links: dict[tuple[str, str], set[str]] = defaultdict(set)
    for label, members in shared:
        if 2 <= len(members) <= max_shared_degree:
            kind = label.split(":", 1)[0]
            for a, b in combinations(sorted(members), 2):
                links[(a, b)].add(kind)
    return links


def build_engagement(store: GraphStore) -> dict[tuple[str, str], float]:
    """Weighted fan -> creator edges from reactions and on-chain tips."""
    w: dict[tuple[str, str], float] = defaultdict(float)
    for fan, posts in store.reactions.items():
        for p in posts:
            author = store.post_author.get(p)
            if author is None:
                continue
            creator = store.resolve(author)
            if creator != fan:
                w[(fan, creator)] += 1.0
    for t in store.tips:
        fan, creator = store.resolve(t["from_wallet"]), store.resolve(t["to_wallet"])
        if fan != creator and not fan.startswith("0x"):
            w[(fan, creator)] += 3.0 * math.log(1 + (t["amount_wei"] / WEI) * 100)
    for p in store.payments.values():
        if p.get("status") == "succeeded" and p["src"] != p["dst"]:
            w[(p["src"], p["dst"])] += 5.0 * math.log(1 + p.get("amount_minor", 0) / 100.0)
    return dict(w)


def personalized_pagerank(edges: dict[tuple[str, str], float], seeds: set[str], damping: float = 0.85,
                          iters: int = 50) -> dict[str, float]:
    """TrustRank on the symmetrised engagement graph: trust flows to anyone a trusted account engages with
    or who engages with them (a plain fan has no inbound edges, so directed-only flow would starve fans)."""
    adj: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for (a, b), x in edges.items():
        adj[a][b] += x
        adj[b][a] += x
    nodes = set(adj) | seeds
    if not nodes or not seeds:
        return {}
    tele = {n: (1 / len(seeds) if n in seeds else 0.0) for n in nodes}
    rank = dict(tele)
    for _ in range(iters):
        nxt = {n: (1 - damping) * tele[n] for n in nodes}
        for n in nodes:
            out = adj.get(n)
            total = sum(out.values()) if out else 0.0
            if total == 0:
                for s in seeds:  # dangling mass returns to the seeds
                    nxt[s] += damping * rank[n] / len(seeds)
                continue
            for m, x in out.items():
                nxt[m] += damping * rank[n] * x / total
        rank = nxt
    return rank


def co_reaction_pairs(store: GraphStore, min_reactions: int = 10, min_jaccard: float = 0.8):
    active = {a: ps for a, ps in store.reactions.items() if len(ps) >= min_reactions}
    out = []
    for a, b in combinations(sorted(active), 2):
        common = len(active[a] & active[b])
        if not common:
            continue
        j = common / (len(active[a]) + len(active[b]) - common)
        if j >= min_jaccard:
            out.append((a, b, common, j))
    return out


def analyse(store: GraphStore, seeds: set[str] | None = None, max_shared_degree: int = 25,
            min_reactions: int = 10, min_jaccard: float = 0.8) -> Analysis:
    seeds = seeds or set()
    feats: dict[str, Feat] = {a: Feat() for a in store.accounts}

    # 1. linkage clusters (WCC over LINKED)
    links = build_linkage(store, max_shared_degree)
    g = nx.Graph()
    for (a, b), reasons in links.items():
        g.add_edge(a, b, reasons=reasons)
    clusters = []
    for i, comp in enumerate(nx.connected_components(g)):
        sigs = sorted({r for a, b in combinations(comp, 2) if g.has_edge(a, b) for r in g[a][b]["reasons"]})
        clusters.append({"members": set(comp), "signals": sigs})
        for a in comp:
            f = feats.setdefault(a, Feat())
            f.linkage_cluster_id, f.linkage_cluster_size = i, len(comp)
            f.reasons.append(f"linked to {len(comp) - 1} other accounts via {', '.join(sigs)}")

    # 2. engagement communities (Louvain)
    eng = build_engagement(store)
    ug = nx.Graph()
    for (a, b), x in eng.items():
        ug.add_edge(a, b, weight=ug[a][b]["weight"] + x if ug.has_edge(a, b) else x)
    communities = []
    if ug.number_of_edges():
        for i, comp in enumerate(nx.community.louvain_communities(ug, weight="weight", seed=7)):
            out_total = defaultdict(float)
            out_in = defaultdict(float)
            for (a, b), x in eng.items():
                if a in comp:
                    out_total[a] += x
                    out_in[a] += x if b in comp else 0.0
            inner = [(a, b) for (a, b) in eng if a in comp and b in comp]
            recip = sum(1 for a, b in inner if (b, a) in eng) / len(inner) if inner else 0.0
            tot = sum(out_total.values())
            ratio = sum(out_in.values()) / tot if tot else 0.0
            communities.append({"members": set(comp), "internal_ratio": ratio, "reciprocity": recip})
            for a in comp:
                f = feats.setdefault(a, Feat())
                f.community_id, f.community_size = i, len(comp)
                f.community_internal_ratio = out_in[a] / out_total[a] if out_total[a] else 0.0
                f.community_reciprocity = recip

    # 3. TrustRank
    rank = personalized_pagerank(eng, seeds)
    if rank:
        mx = max(rank.values()) or 1.0
        for a, f in feats.items():
            f.trustrank = rank.get(a, 0.0)
            f.trustrank_norm = math.log1p(1000 * f.trustrank) / math.log1p(1000 * mx)
    else:
        for f in feats.values():
            f.trustrank_norm = 0.5  # cold start: no seeds, so neutral instead of punishing everyone

    # 4. behavioural clones (Jaccard on reaction sets)
    pairs = co_reaction_pairs(store, min_reactions, min_jaccard)
    cg = nx.Graph()
    cg.add_edges_from((a, b) for a, b, _, _ in pairs)
    groups = [set(c) for c in nx.connected_components(cg)]
    for grp in groups:
        for a in grp:
            feats.setdefault(a, Feat()).reasons.append(f"near-identical reactions to {len(grp) - 1} other accounts")

    return Analysis(feats, clusters, communities, groups, pairs, eng, bool(seeds))
