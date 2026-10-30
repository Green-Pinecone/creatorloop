"""OpenZeppelin-compatible Merkle tree. Leaves are keccak256(keccak256(abi.encode(address, uint256))) and
pairs are hashed in sorted order, which is what MerkleProof.verify expects. An odd node is promoted unchanged,
so its proof simply has one fewer element at that level."""

from eth_abi import encode
from eth_utils import keccak, to_checksum_address


def leaf(addr: str, amount: int) -> bytes:
    return keccak(keccak(encode(["address", "uint256"], [to_checksum_address(addr), amount])))


def _hash_pair(a: bytes, b: bytes) -> bytes:
    return keccak(a + b) if a < b else keccak(b + a)


def build(values: list[tuple[str, int]]) -> tuple[bytes, list[list[bytes]]]:
    """Returns (root, proofs) with proofs in the same order as `values`."""
    if not values:
        raise ValueError("cannot build a tree with no leaves")
    addrs = [a.lower() for a, _ in values]
    if len(set(addrs)) != len(addrs):
        raise ValueError("duplicate address in allocations")
    if any(v <= 0 for _, v in values):
        raise ValueError("amounts must be positive")
    leaves = sorted(leaf(a, v) for a, v in values)
    index = {leaf_: i for i, leaf_ in enumerate(leaves)}
    layers = [leaves]
    while len(layers[-1]) > 1:
        cur, nxt = layers[-1], []
        for i in range(0, len(cur), 2):
            nxt.append(_hash_pair(cur[i], cur[i + 1]) if i + 1 < len(cur) else cur[i])
        layers.append(nxt)
    root = layers[-1][0]

    def proof_for(leaf_: bytes) -> list[bytes]:
        i, proof = index[leaf_], []
        for layer in layers[:-1]:
            sib = i ^ 1
            if sib < len(layer):
                proof.append(layer[sib])
            i //= 2
        return proof

    return root, [proof_for(leaf(a, v)) for a, v in values]


def verify(root: bytes, addr: str, amount: int, proof: list[bytes]) -> bool:
    """Mirror of OZ MerkleProof.verify, for tests and the claims API self-check."""
    h = leaf(addr, amount)
    for p in proof:
        h = _hash_pair(h, p)
    return h == root
