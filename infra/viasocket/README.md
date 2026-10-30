# viaSocket flows

Flows live in viaSocket, not in git. Build each one in the UI, then save a screenshot and notes next to this
file as `F1.md` ... `F6.md`. Until a flow exists, set `VIASOCKET_LOCAL_SINK=true` in `services/api/.env` and
events are written to `.viasocket-sink.jsonl` so you can see exactly what each flow will receive.

## Delivery contract (API -> flow)

`POST <flow webhook URL>`, JSON body `{"event", "id", "data"}`, headers:

| Header | Value |
|---|---|
| `X-CL-Event` | event name |
| `X-CL-Timestamp` | unix seconds |
| `X-CL-Signature` | hex HMAC-SHA256 of `"<timestamp>.<raw body>"` with `VIASOCKET_SIGNING_SECRET` |

`id` is derived from the content, so a retry of the same event has the same `id`; de-duplicate on it.
If the flow builder has a code step, recompute the HMAC and reject mismatches or timestamps older than 5 minutes.
If not, treat the webhook URL as a secret. Payloads never contain emails or other PII.

## Which env var each flow URL goes in

| Env var | Events delivered |
|---|---|
| `VIASOCKET_WEBHOOK_PAYMENT` | `payment.succeeded`, `payment.failed`, `subscription.at_risk` |
| `VIASOCKET_WEBHOOK_FRAUD` | `fraud.case_opened`, `fraud.case_recommended`, `dispute.opened`, `ops.alert`, `epoch.published` |
| `VIASOCKET_WEBHOOK_LEDGER` | `ledger.entry` |
| `VIASOCKET_WEBHOOK_DIGEST` | `digest.creator` |

Set `VIASOCKET_SIGNING_SECRET` and `INTERNAL_API_TOKEN` too (long random strings).

## Event payloads (`data`)

| Event | Fields |
|---|---|
| `payment.succeeded` | `payment_id, kind, creator_id, amount_minor, currency` |
| `payment.failed` | `payment_id` |
| `subscription.at_risk` | `subscription_id, account_id` |
| `fraud.case_opened` | `case_id, kind, severity, size` |
| `dispute.opened` | `payment_id` |
| `ops.alert` | `title, detail, severity` (+ `outbox_id` on dead letters) |
| `epoch.published` | `epoch, onchain_epoch_id, creators, total_loop, excluded, tx` |
| `ledger.entry` | `txn_id, ref, account_type, amount_minor, currency, held` |
| `digest.creator` | `week, handle, display_name, new_supporters, tip_total_wei, tip_count, top_post_id, top_post_reactions` |

Amounts are integer minor units (paise/cents); format them in the flow.

## Flows

**F1 Creator payment notification** (payment webhook). Filter `event == payment.succeeded`; notify the creator
(Discord/Telegram message and an email). Branch `subscription.at_risk` to a "payment needs attention" email.
Done when a test-mode purchase triggers the email in under 60 s.

**F2 Fraud and dispute alert** (fraud webhook). Post to the admin channel with severity, case kind and member
count, linking to `{WEB_BASE_URL}/admin/cases/{case_id}`. No approve button in the alert: decisions are made in
the admin console. Severity >= 5: also email the on-call admin.

**F3 Trust + epoch scheduler** (cron, 15 min demo / hourly prod). `POST {API}/internal/trust/run` with header
`X-Internal-Token`. On the epoch schedule (30 min demo / weekly prod) also `POST /internal/epochs/run`
(scores, publishes the Merkle root on-chain, returns a summary; safe to call twice for one window). Post the JSON summary to `#ops`; alert on non-2xx.

**F4 Ledger mirror** (ledger webhook). Append `ts, txn_id, ref, account_type, amount_minor, currency, held` to
the "CreatorLoop Ledger (TEST)" Google Sheet. Duplicate rows on retry are harmless.

**F5 Weekly creator digest** (cron, Monday 09:00 IST). `POST /internal/digests/creators`; the API then emits one
`digest.creator` per creator (once per ISO week) into a second webhook flow that sends the email.

**F6 Maintenance** (cron, daily). `POST /internal/maintenance/retention` and `POST /internal/ledger/check`,
then post both summaries to `#ops`.

## Internal endpoints (viaSocket -> API)

All need header `X-Internal-Token: $INTERNAL_API_TOKEN`: `POST /internal/trust/run`, `/internal/epochs/run`,
`/internal/digests/creators`, `/internal/maintenance/retention`, `/internal/ledger/check`. All are idempotent.

## Fallback

If viaSocket is unreachable, set `LOCAL_SCHEDULER=true`: the API runs trust (15 min), ledger check (hourly),
retention (daily) and digests (Monday) itself. It also runs epochs on the same cadence as `EPOCH_WINDOW_MINUTES`.

## Agents (Stage 09)

If you expose a viaSocket MCP server to the agents, allowlist only: post to `#trust-cases`, append to the
"Case Log" sheet, email a templated message to the admin. Never expose anything that moves money, changes
account status or posts publicly.
