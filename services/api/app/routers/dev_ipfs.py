"""Local stand-in for the Pinata pipeline (enabled with DEV_IPFS=true). Same contract as the Next.js routes:
upload media -> media CID, then submit metadata -> metadata CID. `author` is taken from the authenticated
account's linked wallet, never from the request body."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from .. import ipfs
from ..auth import current_account, ensure_active
from ..config import get_settings
from ..db.models import Account
from ..metadata import Media, PostMetadataV1, clean_caption, parse_metadata


def _enabled() -> None:
    if not get_settings().dev_ipfs:
        raise HTTPException(404, "not found")


router = APIRouter(prefix="/dev/ipfs", tags=["dev-ipfs"], dependencies=[Depends(_enabled)])
_uploads: dict[str, list[float]] = {}


def _rate_limit(key: str, max_: int = 20, window: int = 3600) -> None:
    import time

    now = time.time()
    hits = [t for t in _uploads.get(key, []) if now - t < window]
    if len(hits) >= max_:
        raise HTTPException(429, "upload rate limit")
    _uploads[key] = [*hits, now]


@router.post("/media")
async def upload_media(request: Request, acct: Account = Depends(current_account)):
    ensure_active(acct)
    _rate_limit(f"upload:{acct.id}")
    data = await request.body()
    if not data or len(data) > ipfs.MAX_MEDIA_BYTES:
        raise HTTPException(413, "file empty or larger than 10 MB")
    mime = ipfs.sniff_mime(data)  # trust content, not the Content-Type header
    if mime is None:
        raise HTTPException(415, "unsupported media type")
    if mime == "image/gif" and len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "gifs are limited to 5 MB")
    return {"cid": ipfs.put_local(data), "mime": mime, "bytes": len(data)}


class MetadataIn(BaseModel):
    media: Media
    caption: str = ""
    tags: list[str] = Field(default_factory=list)


@router.post("/metadata")
async def submit_metadata(body: MetadataIn, acct: Account = Depends(current_account)):
    ensure_active(acct)
    if not acct.wallet_address:
        raise HTTPException(403, "link a wallet first")
    _rate_limit(f"meta:{acct.id}", max_=60)
    blob = ipfs.get_local(body.media.cid)
    if blob is None:
        raise HTTPException(400, "media not found")
    if ipfs.sniff_mime(blob) != body.media.mime or len(blob) != body.media.bytes:
        raise HTTPException(400, "media does not match declared mime/size")
    import hashlib

    if hashlib.sha256(blob).hexdigest() != body.media.sha256:
        raise HTTPException(400, "media sha256 mismatch")
    try:
        meta = PostMetadataV1.model_validate({
            "schema": "creatorloop.post.v1", "kind": "image", "media": body.media.model_dump(),
            "caption": clean_caption(body.caption), "tags": body.tags,
            "createdAt": datetime.now(UTC).isoformat(), "author": acct.wallet_address,
        })
        raw = meta.model_dump_json(by_alias=True).encode()
        parse_metadata(raw)
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, str(e)[:300]) from e
    return {"metadataCid": ipfs.put_local(raw)}


@router.get("/{cid}")
async def gateway(cid: str):
    data = ipfs.get_local(cid)
    if data is None:
        raise HTTPException(404, "not found")
    mime = ipfs.sniff_mime(data) or "application/json"
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=31536000, immutable",
                                                    "X-Content-Type-Options": "nosniff"})
