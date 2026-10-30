"""IPFS access for the API: CID computation/verification, a local content-addressed store (dev stand-in for
Pinata) and gateway fetch. Metadata and media are stored as raw-codec CIDv1 blocks (bafkrei...), so the
hash in the CID can be re-checked against whatever a gateway returns."""

import base64
import hashlib
import re
from pathlib import Path

import httpx

from .config import get_settings

CID_RE = re.compile(r"^b[a-z2-7]{58,}$")
MAX_METADATA_BYTES = 16 * 1024
MAX_MEDIA_BYTES = 10 * 1024 * 1024
FALLBACK_GATEWAY = "https://ipfs.io"


def cid_for(data: bytes) -> str:
    """CIDv1, raw codec (0x55), sha2-256, base32 lower-case. Matches `ipfs add --raw-leaves --cid-version 1`
    for content that fits in one block (<= 256 KiB)."""
    digest = hashlib.sha256(data).digest()
    return "b" + base64.b32encode(bytes([0x01, 0x55, 0x12, 0x20]) + digest).decode().lower().rstrip("=")


def verify(cid: str, data: bytes) -> bool:
    return CID_RE.match(cid) is not None and cid_for(data) == cid


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _store_dir() -> Path:
    d = Path(get_settings().ipfs_local_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def put_local(data: bytes) -> str:
    cid = cid_for(data)
    (_store_dir() / cid).write_bytes(data)
    return cid


def get_local(cid: str) -> bytes | None:
    if not CID_RE.match(cid):
        return None
    p = _store_dir() / cid
    return p.read_bytes() if p.exists() else None


async def fetch(cid: str, max_bytes: int = MAX_METADATA_BYTES) -> bytes:
    """Local store first, then the configured gateway, then the public fallback. The result is always
    checked against the CID, so a misbehaving gateway cannot substitute content."""
    if not CID_RE.match(cid):
        raise ValueError("invalid cid")
    if (data := get_local(cid)) is not None and verify(cid, data):
        return data
    gateways = [g for g in (get_settings().ipfs_gateway_url, FALLBACK_GATEWAY) if g]
    last: Exception | None = None
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        for g in gateways:
            try:
                r = await c.get(f"{g.rstrip('/')}/ipfs/{cid}")
                r.raise_for_status()
                if len(r.content) > max_bytes:
                    raise ValueError("content too large")
                if not verify(cid, r.content):
                    raise ValueError("content does not match cid")
                return r.content
            except (httpx.HTTPError, ValueError) as e:
                last = e
    raise RuntimeError(f"could not fetch {cid}: {last}")
