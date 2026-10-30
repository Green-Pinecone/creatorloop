"""Post metadata v1 (mirrors packages/shared/src/metadata.ts) and the ipfs.fetch_metadata outbox handler."""

import re
import unicodedata
from datetime import datetime
from typing import Literal

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from .db.models import Post
from .db.session import session_maker
from .ipfs import fetch
from .outbox import register

log = structlog.get_logger()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_GIF_BYTES = 5 * 1024 * 1024


class Media(BaseModel):
    cid: str = Field(pattern=r"^b[a-z2-7]{58,}$")
    mime: Literal["image/webp", "image/jpeg", "image/png", "image/gif"]
    width: int = Field(gt=0, le=8000)
    height: int = Field(gt=0, le=8000)
    bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PostMetadataV1(BaseModel):
    schema_: Literal["creatorloop.post.v1"] = Field(alias="schema")
    kind: Literal["image"]
    media: Media
    caption: str = Field(max_length=500)
    tags: list[str] = Field(max_length=5)
    createdAt: datetime
    author: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        if not all(re.fullmatch(r"[a-z0-9-]{1,24}", t) for t in v):
            raise ValueError("bad tag")
        return v


def clean_caption(text: str) -> str:
    return _CONTROL.sub("", unicodedata.normalize("NFC", text)).strip()


def parse_metadata(raw: bytes) -> PostMetadataV1:
    meta = PostMetadataV1.model_validate_json(raw)
    if meta.media.mime == "image/gif" and meta.media.bytes > MAX_GIF_BYTES:
        raise ValueError("gif too large")
    return meta


@register("ipfs.fetch_metadata")
async def fetch_metadata(payload: dict) -> None:
    """Cache caption + media hash on the post. Bad metadata flags the post instead of failing the queue:
    retrying will not make invalid JSON valid. Network failures raise, so the outbox retries them."""
    post_id, cid = payload["post_id"], payload["cid"]
    try:
        meta = parse_metadata(await fetch(cid))
    except (ValidationError, ValueError) as e:
        log.warning("metadata.invalid", post_id=post_id, error=str(e)[:200])
        meta = None
    async with session_maker()() as s, s.begin():
        post = await s.get(Post, post_id)
        if post is None:
            return
        if meta is None or meta.author.lower() != post.author_wallet:  # author must match the on-chain sender
            post.moderation = "flagged" if post.moderation == "pending" else post.moderation
            return
        post.caption = clean_caption(meta.caption)
        post.media_sha256 = meta.media.sha256
