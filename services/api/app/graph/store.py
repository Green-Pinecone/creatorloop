"""In-memory trust graph. Same logical model as the TigerGraph schema in the plan (Account, Wallet, Device,
NetBucket, PayerId, Post and their edges) so the analysis in features.py can later be swapped for installed
GSQL queries. Postgres stays the source of truth; this is a derived index rebuilt by graph/sync.py."""

from collections import defaultdict


class GraphStore:
    SIGNAL_KINDS = ("Device", "NetBucket", "PayerId")

    def __init__(self) -> None:
        self.accounts: set[str] = set()
        self.wallet_owner: dict[str, str] = {}  # wallet -> account
        self.signals: dict[str, dict[str, set[str]]] = {k: defaultdict(set) for k in self.SIGNAL_KINDS}
        self.funded_by: dict[str, str] = {}  # wallet -> funder wallet
        self.post_author: dict[str, str] = {}  # post id -> account id or (unlinked) wallet
        self.reactions: dict[str, set[str]] = defaultdict(set)  # account -> post ids
        self.follows: set[tuple[str, str]] = set()
        self.tips: list[dict] = []  # {from_wallet, to_wallet, post_id, amount_wei}
        self.payments: dict[str, dict] = {}  # payment_id -> {src, dst, amount_minor, status, product}

    def clear(self) -> None:
        self.__init__()

    def apply(self, ev: dict) -> None:
        """Idempotent: replaying an event never changes the result."""
        kind = ev["kind"]
        src, dst = ev.get("src"), ev.get("dst")
        if kind == "REACTED":
            self.accounts.add(src)
            self.reactions[src].add(str(dst))
        elif kind == "UNREACTED":
            self.reactions[src].discard(str(dst))
        elif kind == "FOLLOWS":
            self.accounts.update((src, dst))
            self.follows.add((src, dst))
        elif kind == "USED_DEVICE":
            self.accounts.add(src)
            self.signals["Device"][dst].add(src)
        elif kind == "SEEN_ON_NET":
            self.accounts.add(src)
            self.signals["NetBucket"][dst].add(src)
        elif kind == "PAID_WITH":
            self.accounts.add(src)
            self.signals["PayerId"][dst].add(src)
        elif kind == "OWNS_WALLET":
            self.accounts.add(src)
            self.wallet_owner[dst.lower()] = src
        elif kind == "FUNDED_BY":
            self.funded_by[src.lower()] = dst.lower()
        elif kind == "AUTHORED":
            self.post_author[str(dst)] = src
        elif kind == "TIPPED":
            key = (ev.get("tx_hash"), ev.get("log_index"))
            rec = {"from_wallet": src.lower(), "to_wallet": dst.lower(), "post_id": str(ev.get("post_id")),
                   "amount_wei": int(ev.get("amount_wei", 0)), "key": key}
            if key == (None, None) or not any(t["key"] == key for t in self.tips):
                self.tips.append(rec)
        elif kind == "PAID":
            self.accounts.update((src, dst))
            self.payments[ev["payment_id"]] = ev  # idempotent; a refund overwrites with the new status

    def resolve(self, node: str) -> str:
        """Map a wallet address to its owning account when linked; accounts pass through."""
        return self.wallet_owner.get(node.lower(), node) if node.startswith("0x") else node
