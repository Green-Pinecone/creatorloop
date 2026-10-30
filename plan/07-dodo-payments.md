# Stage 07: Dodo Payments (Revenue Engine + Creator Fund)

**Goal:** fans pay with cards or UPI through Dodo Payments' merchant-of-record checkout:
- **Supporter Pass** subscriptions to a specific creator,
- **Boost Packs** (one-time purchase) to promote posts.

Verified webhooks drive a double-entry ledger that credits each creator's **creator fund**. Payment events also feed TigerGraph, so payment fraud is caught by the same trust engine.

**Prerequisites:** Stage 04 (API, outbox, webhook idempotency table). A Dodo account in **test mode** (Stage 01).
**Outputs:** products configured, `POST /checkout/*` endpoints, `POST /webhooks/dodo` with signature verification, `payments`/`subscriptions`/`ledger_entries` tables, the creator-fund balance API, and outbox events to TigerGraph and viaSocket.

---

## 1. Products (Dodo dashboard, test mode)

| Product | Type | Price (test) | Metadata we attach at checkout |
|---|---|---|---|
| **Supporter Pass** | Subscription, monthly | ₹149 / $2.99 | `creator_id`, `account_id`, `kind=supporter_pass` |
| **Boost Pack** | One-time | ₹99 / $1.49 | `account_id`, `post_id`, `kind=boost` |
| *(stretch)* **Creator Pro** | Subscription | ₹299 / $4.99 | `account_id`, `kind=creator_pro` (unlocks Copilot quotas, Stage 09) |

Record the product ids in `DODO_PRODUCT_SUPPORTER_PASS` and `DODO_PRODUCT_BOOST_PACK`.

**Why one shared Supporter Pass product** instead of one per creator: the creator is carried in `metadata.creator_id`, so creators can join without dashboard work. Per-creator pricing is a later feature (adaptive pricing or multiple price tiers).

## 2. Money model

```
Fan pays ₹149 (Supporter Pass for creator C)
   └─ Dodo (merchant of record) deducts fees + tax → net amount settles to the platform
        └─ Ledger split on payment.succeeded:
             70% → creator_fund:C    (CREATOR_FUND_SHARE_BPS = 7000)
             30% → platform_revenue
Boost Pack ₹99 → 100% platform_revenue; post P gets boost_until = now + 24h (feed ranking)
```

- Compute the split on the **net** amount if Dodo's payload exposes it. Otherwise use the gross amount minus your configured fee estimate, and reconcile against Dodo payouts later. Check which amount fields the `payment.succeeded` payload includes.
- **Creator-fund payouts** to creators are **out of scope for the hackathon**. Paying third parties needs a payout or marketplace product and creator KYC. The fund is shown as an accrued balance, "paid monthly after verification". Say this plainly in the pitch, and mention it as the post-hackathon roadmap (Dodo's own payout or marketplace features if offered, or a separate payout provider).
- Funds from accounts in an open fraud case stay **held**: entries are tagged `held=true` until the case closes.

## 3. Database

```sql
CREATE TABLE payments (
  dodo_payment_id  text PRIMARY KEY,
  account_id       uuid REFERENCES accounts(id),
  creator_id       uuid REFERENCES accounts(id),
  kind             text NOT NULL,                  -- supporter_pass | boost | creator_pro
  status           text NOT NULL,                  -- succeeded | failed | refunded | disputed
  amount_minor     bigint NOT NULL,
  currency         text NOT NULL,
  dodo_customer_id text,
  payer_hash       text,                           -- HMAC(salt, customer email) → PayerId vertex
  subscription_id  text,
  post_id          bigint,
  raw              jsonb NOT NULL,
  created_at       timestamptz NOT NULL
);

CREATE TABLE subscriptions (
  dodo_subscription_id text PRIMARY KEY,
  account_id   uuid REFERENCES accounts(id),
  creator_id   uuid REFERENCES accounts(id),
  status       text NOT NULL,                      -- active | on_hold | cancelled | expired | failed
  current_period_end timestamptz,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ledger_entries (                      -- double entry: every txn sums to 0
  id          bigserial PRIMARY KEY,
  txn_id      uuid NOT NULL,
  account     text NOT NULL,                       -- 'cash:dodo' | 'creator_fund:<uuid>' | 'platform_revenue' | 'refunds'
  amount_minor bigint NOT NULL,                    -- +credit / −debit
  currency    text NOT NULL,
  held        boolean NOT NULL DEFAULT false,
  ref         text NOT NULL,                       -- dodo_payment_id / refund id
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON ledger_entries (account);
-- invariant check (CI + nightly): SELECT txn_id FROM ledger_entries GROUP BY txn_id HAVING sum(amount_minor) <> 0  → must be empty
```

## 4. Checkout endpoints

`app/payments/dodo.py`:

```python
from dodopayments import AsyncDodoPayments

client = AsyncDodoPayments(bearer_token=settings.dodo_payments_api_key,
                           environment=settings.dodo_environment)   # "test_mode" | "live_mode"
```

`POST /checkout/supporter-pass` with body `{creator_handle}`:

```python
@router.post("/checkout/supporter-pass")
async def supporter_checkout(body: SupporterCheckout, acct = Depends(current_account), s = Depends(get_session)):
    ensure_active(acct)
    creator = await get_creator_by_handle(s, body.creator_handle) or abort(404)
    if creator.account_id == acct.id: abort(400, "cannot support yourself")
    if await has_active_subscription(s, acct.id, creator.account_id): abort(409, "already supporting")
    if acct.trust_tier == "quarantine": abort(403, "account under review")

    session = await client.checkout_sessions.create(
        product_cart=[{"product_id": settings.dodo_product_supporter_pass, "quantity": 1}],
        customer={"email": acct.email or None, "name": acct.display_name or "CreatorLoop fan"},
        return_url=f"{settings.web_base_url}/c/{body.creator_handle}?checkout=done",
        metadata={"kind": "supporter_pass", "account_id": str(acct.id), "creator_id": str(creator.account_id)},
    )
    return {"checkout_url": session.checkout_url, "session_id": session.session_id}
```

`POST /checkout/boost` with `{post_id}` works the same way: `kind=boost`, `post_id`, and checks that the caller authored the post.

Notes from the Dodo docs:
- Checkout Sessions are the recommended integration. The response always has `session_id`, and `checkout_url` is what you redirect to.
- After payment, the customer returns to `return_url` with `payment_id` and `status` query params. **Never fulfil from the return URL.** Use it only to show "processing…" until the webhook lands. **Webhooks are the source of truth.**
- Confirm the `metadata` field name and its value limits (strings only, size) in the API reference, and keep values short (UUIDs).

## 5. Webhook handler

Dodo signs webhooks following the **Standard Webhooks** spec: headers `webhook-id`, `webhook-timestamp`, `webhook-signature`, with the secret from Developer → Webhooks. Verify on the **raw body**.

```python
from standardwebhooks.webhooks import Webhook

wh = Webhook(settings.dodo_payments_webhook_secret)

@router.post("/webhooks/dodo")
async def dodo_webhook(request: Request, s = Depends(get_session)):
    raw = await request.body()
    headers = {k: request.headers.get(k) for k in ("webhook-id", "webhook-timestamp", "webhook-signature")}
    try:
        event = wh.verify(raw, headers)          # raises on bad signature / stale timestamp
    except Exception:
        raise HTTPException(401, "bad signature")

    async with s.begin():
        fresh = await insert_webhook_event_if_absent(s, "dodo", headers["webhook-id"], event)
        if not fresh:
            return {"ok": True, "duplicate": True}     # idempotent: Dodo retries
        await dispatch(s, event)                       # all side effects in the same txn (+ outbox rows)
    return {"ok": True}
```

> Check the exact Python import path of the Standard Webhooks library and whether the Dodo SDK ships its own `unwrap`/verify helper. Use whichever the Dodo docs show. Respond 2xx quickly; heavy work goes to the outbox.

### Event → effect map

Verify the event type names against Dodo's webhook reference. The commonly documented set is below.

| Event | Effect |
|---|---|
| `payment.succeeded` | Upsert `payments` (succeeded). If `kind=supporter_pass`, ledger split 70/30 (`held` if payer or creator is in an open case). If `kind=boost`, set `posts.boost_until`. Enqueue `graph.upsert` (`PAID`, `PAID_WITH` PayerId) and `viasocket.emit payment.succeeded`. |
| `payment.failed` | Upsert `payments` (failed). Enqueue a `graph.upsert` attribute bump (many failures from one PayerId means card testing). |
| `subscription.active` / `subscription.renewed` | Upsert `subscriptions`; extend `current_period_end`; supporter badge on. |
| `subscription.on_hold` / `subscription.failed` | Status update; badge off after a grace period; `viasocket.emit subscription.at_risk` to the fan. |
| `subscription.cancelled` / `subscription.expired` | Status update; badge off at period end. |
| `refund.succeeded` | Reversing ledger txn (debit creator_fund, credit refunds); `payments.status=refunded`; graph edge status refunded. |
| `dispute.opened` (and related dispute.* events) | `payments.status=disputed`; hold the creator's fund entries for that payment; `viasocket.emit dispute.opened` to the admin; feeds the `payment_abuse` case rule. |

Unknown event types: store them and return 200.

### Mapping identities

- `metadata.account_id` / `creator_id` link the payment to our accounts. If metadata is missing (for example on renewal events), look up via `subscription_id` in our `subscriptions` table.
- `payer_hash = HMAC(salt, lower(customer.email))` becomes a `PayerId` vertex in TigerGraph. One card or email paying from many accounts to the same creator is a classic self-dealing pattern.

## 6. Creator fund API

| Path | Returns |
|---|---|
| `GET /me/creator-fund` | `{available_minor, held_minor, lifetime_minor, currency, last_entries[]}` |
| `GET /creators/{handle}/supporters` | Count + top supporters (opt-in display) |
| `GET /admin/ledger/summary` | Totals by account type; invariant check status |

`available = SUM(amount) WHERE account='creator_fund:<id>' AND NOT held`.

## 7. Fraud integration (with Stage 05)

Payment-specific signals added to the trust model:
- `refund_or_dispute_count` > 0: a negative component.
- **Clean payer** (≥ 1 succeeded payment, no disputes, account older than 7 days): eligible as a TrustRank seed. Real money is a strong anti-sybil signal.
- **Self-dealing:** PayerId or Device shared between the fan and the creator they pay opens a `payment_abuse` case and holds the funds.
- **Card testing:** more than 5 `payment.failed` from one device/PayerId in 10 min. Block checkout creation for that account for 1 h (checked in `/checkout/*`).

## 8. Testing

- **Unit:** signature verification with known-good and tampered fixtures (generate them with the Standard Webhooks library and a test secret); a duplicate `webhook-id` gives a no-op; ledger split rounding (₹149 × 70% = 104.30, so store 10430 paise, with the remainder to platform); the refund reversal sums to zero.
- **Integration (test mode):**
  1. Create a checkout from the API.
  2. Open the URL and pay with Dodo's test card (see Dodo docs for test card numbers / UPI test flows).
  3. Watch the webhook arrive via the tunnel; assert the ledger and graph edge.
  4. Trigger a refund from the dashboard; assert the reversal.
- **Replay:** use Dodo's dashboard "resend webhook" (if available) to confirm idempotency.
- **Ledger invariant** runs in CI and as a nightly job; an alert goes out via viaSocket if it ever fails.

## 9. Acceptance checklist

- [ ] Supporter Pass test purchase → ledger shows the 70/30 split → creator sees the fund balance → viaSocket notification fires.
- [ ] Boost purchase → post marked boosted for 24 h in the feed.
- [ ] Refund → balances revert exactly.
- [ ] A bad signature gets 401; a duplicate delivery is a no-op.
- [ ] A self-dealing test (same device pays own second account's creator page) opens a `payment_abuse` case with funds held.
- [ ] No floats anywhere in money code (grep for `float(` in `payments/`).

## 10. Pitfalls

- **Fulfilling from the redirect** instead of the webhook leads to double-fulfilment and spoofable upgrades.
- **Test vs live keys:** keep `DODO_ENVIRONMENT=test_mode` until after the hackathon. Separate webhook secrets per environment.
- **Tunnel restarts** change the webhook URL. Use a stable tunnel domain (see Stage 01).
- **Currency:** Dodo may present local currency to the buyer (adaptive pricing). Store `currency` per payment and never sum across currencies without conversion.
