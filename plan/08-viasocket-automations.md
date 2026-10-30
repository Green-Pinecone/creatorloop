# Stage 08: viaSocket Automations

**Goal:** every operational side effect (notifications, admin alerts, scheduled jobs, finance mirrors, digests) runs as a **visible, editable viaSocket flow** instead of hidden code. The API emits signed events; viaSocket flows fan them out to Discord, Telegram, Email and Google Sheets, and call back into the API on a schedule. The viaSocket MCP server also gives the AI agents (Stage 09) a safe way to take actions in external apps.

**Prerequisites:** Stage 04 (outbox), Stage 05 (cases), Stage 07 (payment events). A viaSocket workspace (Stage 01).
**Outputs:** `app/automations/events.py` (signed emitter), `/internal/*` endpoints (token-gated), the six flows below built and tested in viaSocket, and screenshots plus notes in `infra/viasocket/`.

---

## 1. Integration pattern

```
API (outbox topic viasocket.emit)
   │  POST <flow webhook URL>
   │  headers: X-CL-Event, X-CL-Timestamp, X-CL-Signature (HMAC-SHA256 over "<ts>.<body>")
   ▼
viaSocket flow ── [Webhook trigger] → [optional: verify/filter step] → [actions: Discord/Telegram/Email/Sheets/HTTP]

viaSocket flow ── [Cron trigger] → [HTTP action: POST {API}/internal/<job>  header X-Internal-Token]
                                           ▼
                                      API runs job, returns summary JSON → flow posts summary to #ops
```

Why this split:
- **Outbound** events are fire-and-forget from the API's point of view. The outbox retries until viaSocket returns 2xx.
- **Inbound** calls from viaSocket are limited to a handful of `/internal/*` endpoints, each idempotent and authenticated with a static token.

## 2. Emitter

`app/automations/events.py`:

```python
FLOW_URLS = {
    "payment.succeeded":     settings.viasocket_webhook_payment,
    "subscription.at_risk":  settings.viasocket_webhook_payment,
    "fraud.case_opened":     settings.viasocket_webhook_fraud,
    "fraud.case_recommended": settings.viasocket_webhook_fraud,
    "dispute.opened":        settings.viasocket_webhook_fraud,
    "ledger.entry":          settings.viasocket_webhook_ledger,
    "digest.creator":        settings.viasocket_webhook_digest,
    "ops.alert":             settings.viasocket_webhook_fraud,
}

async def emit(payload: dict):
    event = payload["event"]
    body = json.dumps({"event": event, "id": payload["id"], "data": payload["data"]}, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hmac.new(settings.viasocket_signing_secret.encode(), f"{ts}.{body}".encode(), "sha256").hexdigest()
    r = await http.post(FLOW_URLS[event], content=body, headers={
        "content-type": "application/json", "X-CL-Event": event, "X-CL-Timestamp": ts, "X-CL-Signature": sig})
    r.raise_for_status()        # outbox retries on failure
```

Event payloads carry **no secrets and minimal PII**: handles and display names, never emails, and amounts in display format plus minor units. Each event has a stable `id` so flows can de-duplicate.

**Verifying inside viaSocket:** if the flow builder supports a code or JS step, recompute the HMAC and stop the flow on mismatch. If it doesn't, rely on the secrecy of the flow URL (treat it as a secret, rotate if leaked) and keep payloads non-sensitive. Check what viaSocket's webhook trigger offers (custom header checks, code steps) when you build the first flow.

## 3. Internal endpoints (called by viaSocket)

```python
def require_internal(x_internal_token: str = Header(...)):
    if not hmac.compare_digest(x_internal_token, settings.internal_api_token):
        raise HTTPException(401)
```

| Endpoint | Job | Idempotency |
|---|---|---|
| `POST /internal/trust/run` | Stage 05 trust job | Skips if a run finished under 5 min ago |
| `POST /internal/epochs/run` | Stage 11 epoch close → score → Merkle → publish | Keyed by epoch window; re-calls return the existing result |
| `POST /internal/digests/creators` | Builds weekly creator digests; emits `digest.creator` per creator | Keyed by ISO week |
| `POST /internal/maintenance/retention` | Deletes `device_signals` older than 90 days | Naturally idempotent |
| `POST /internal/ledger/check` | Runs the ledger invariant; emits `ops.alert` if broken | Naturally idempotent |

Each returns a short JSON summary that the flow forwards to the ops channel. For example: `{"epoch": 12, "creators": 31, "total_loop": "10000", "excluded_frozen": 8, "tx": "0x…"}`.

## 4. Flows to build

Build each in the viaSocket UI. Save a screenshot and a short description in `infra/viasocket/<flow>.md`, because flows live in viaSocket, not in git.

### F1: Creator payment notification
- **Trigger:** webhook (`payment.succeeded`, `subscription.at_risk`)
- **Steps:**
  1. Filter: `event == payment.succeeded`.
  2. Discord or Telegram message to the creator community channel: "🎉 @{creator} got a new Supporter!" (opt-in creators only; the flag is in the payload).
  3. Email to the creator ("You have a new supporter. Fund balance: ₹X").
  4. Branch on `subscription.at_risk`: email the fan "Your Supporter Pass payment needs attention".
- **Acceptance:** a test-mode purchase triggers the email in under 60 s.

### F2: Fraud and dispute alert
- **Trigger:** webhook (`fraud.case_opened`, `fraud.case_recommended`, `dispute.opened`, `ops.alert`)
- **Steps:**
  1. Post to the admin Discord or Slack channel with severity colour, case kind, member count, and the agent summary (when present).
  2. Include a link to `{WEB}/admin/cases/{id}`. The admin decides there; the alert itself has **no** approve button, which keeps it safe.
  3. If severity is 5 or more, also email the on-call admin.
- **Acceptance:** the attack simulation (Stage 13) produces an alert with an agent summary.

### F3: Trust + epoch scheduler
- **Trigger:** cron. Every 15 min in the demo, hourly in "prod" for trust. Every 30 min in the demo, weekly in "prod" for epochs.
- **Steps:**
  1. HTTP POST `/internal/trust/run`.
  2. On the epoch schedule, HTTP POST `/internal/epochs/run`.
  3. Post the summary to `#ops`.
  4. On a non-2xx response, post a failure alert.
- **Fallback:** keep an `APScheduler` job in the API, disabled by default (`LOCAL_SCHEDULER=true` enables it), in case viaSocket is unreachable on demo day.

### F4: Ledger mirror to Google Sheets
- **Trigger:** webhook (`ledger.entry`, emitted per ledger txn)
- **Steps:** append a row (`ts, txn_id, kind, account_type, amount, currency, held`) to the "CreatorLoop Ledger (TEST)" sheet.
- **Why:** a finance-friendly audit view, and judges like seeing money flow somewhere familiar.

### F5: Weekly creator digest
- **Trigger:** cron (Monday 09:00 IST) → HTTP POST `/internal/digests/creators`. The API then emits `digest.creator` events into a second webhook flow.
- **Steps (digest webhook flow):**
  1. Optionally, the Copilot agent (Stage 09) writes a 3-line personalised summary; the API includes it in the payload.
  2. Send the email: new supporters, LOOP earned, top post, tip total.

### F6: Maintenance
- **Trigger:** cron (daily).
- **Steps:** POST `/internal/maintenance/retention` and `/internal/ledger/check`, then post the summary to `#ops`.

## 5. viaSocket MCP for agents (optional, high-impact)

viaSocket exposes MCP servers that let AI tools call actions in connected apps. Use it to give the **Fraud Investigator** and **Copilot** agents a constrained action surface without writing per-app integrations:

- Create an MCP configuration in viaSocket that exposes only these **allowlisted** actions:
  - post a message to the `#trust-cases` channel,
  - create a row in the "Case Log" sheet,
  - send a templated email to the admin.
- In Stage 09, register the MCP endpoint as a tool provider for the agent runtime (or call the same flows via webhooks if your model gateway lacks MCP client support).
- **Never** expose actions that move money, change account status or post publicly to creators. Those stay in the API behind human approval.

> Check viaSocket's MCP docs for how the server URL and auth are issued (per-user or per-workspace) and what client transports are supported. Treat the MCP URL as a secret.

## 6. Tests

- **Unit:** the signature over `ts.body` matches a reference implementation; the event-to-URL mapping is complete (every emitted event has a URL); the payload has no `email` keys (a test that scans for PII keys).
- **Contract:** `/internal/*` rejects a missing or wrong token (401); repeated calls are idempotent.
- **Live:** a script `scripts/fire_test_events.py` sends one of each event to the real flows; tick each in a checklist.

## 7. Acceptance checklist

- [ ] F1–F6 built, enabled, screenshotted and documented in `infra/viasocket/`.
- [ ] End-to-end: Dodo test payment → email within 60 s.
- [ ] End-to-end: attack sim → admin alert with agent summary.
- [ ] Cron-driven trust and epoch runs post summaries to `#ops` for at least 2 consecutive cycles.
- [ ] Turning off viaSocket and enabling `LOCAL_SCHEDULER` keeps epochs running (fallback tested).

## 8. Pitfalls

- **Free-tier limits** on runs or tasks: the demo-frequency crons (15/30 min) eat quota. Switch them to demo cadence only on demo day.
- **Flow URL leakage** lets anyone send fake alerts. Rotate if leaked; keep payloads non-actionable (links to the admin console, not one-click actions).
- **Duplicate deliveries:** flows may retry. Include the event `id` and make destinations tolerant (Sheets duplicates are harmless; emails should be de-duplicated by including the id in a hidden header or by tracking it in the API).
