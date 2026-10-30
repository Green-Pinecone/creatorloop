"""Outbound viaSocket events: the `viasocket.emit` outbox topic. Each delivery is HMAC-signed over
"<timestamp>.<body>" and carries a stable id so flows can de-duplicate retries. Payloads contain no secrets and
no emails; only ids, handles and amounts."""

import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import structlog

from ..config import Settings, get_settings
from ..outbox import register

log = structlog.get_logger()
PII_KEYS = {"email", "email_hash", "payer_hash", "name", "phone", "ip"}

# event -> name of the Settings field holding the flow's webhook URL
FLOW_FIELDS = {
    "payment.succeeded": "viasocket_webhook_payment",
    "payment.failed": "viasocket_webhook_payment",
    "subscription.at_risk": "viasocket_webhook_payment",
    "fraud.case_opened": "viasocket_webhook_fraud",
    "fraud.case_recommended": "viasocket_webhook_fraud",
    "dispute.opened": "viasocket_webhook_fraud",
    "ops.alert": "viasocket_webhook_fraud",
    "epoch.published": "viasocket_webhook_fraud",  # posted to #ops by flow F3
    "ledger.entry": "viasocket_webhook_ledger",
    "digest.creator": "viasocket_webhook_digest",
}


def sign_body(secret: str, ts: str, body: str) -> str:
    return hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


def build(payload: dict) -> tuple[str, str]:
    """(event name, compact JSON body). The id is derived from the content, so a retry of the same outbox row
    always carries the same id."""
    event = payload["event"]
    data = {k: v for k, v in payload.items() if k != "event"}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    ident = hashlib.sha256(f"{event}|{canonical}".encode()).hexdigest()[:24]
    return event, json.dumps({"event": event, "id": ident, "data": data}, separators=(",", ":"), default=str)


def flow_url(event: str, st: Settings) -> str | None:
    field = FLOW_FIELDS.get(event)
    return getattr(st, field) if field else None


def _write_sink(st: Settings, event: str, body: str) -> None:
    path = Path(st.viasocket_sink_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": event, "body": json.loads(body)}) + "\n")


@register("viasocket.emit")
async def emit(payload: dict) -> None:
    st = get_settings()
    event, body = build(payload)
    if event not in FLOW_FIELDS:
        raise ValueError(f"unknown viaSocket event {event!r}")  # a code bug: surfaces via retries + dead letter
    if st.viasocket_local_sink:
        _write_sink(st, event, body)  # inspect events without any viaSocket account
        return
    if not st.viasocket_signing_secret:
        log.info("viasocket.unconfigured_drop", kind=event)  # nothing to deliver to; do not retry forever
        return
    url = flow_url(event, st)
    if not url:
        raise RuntimeError(f"no flow URL configured for {event}")
    ts = str(int(time.time()))
    headers = {"content-type": "application/json", "X-CL-Event": event, "X-CL-Timestamp": ts,
               "X-CL-Signature": sign_body(st.viasocket_signing_secret, ts, body)}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, content=body, headers=headers)
    r.raise_for_status()  # non-2xx -> the outbox retries with backoff
