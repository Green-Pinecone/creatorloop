# CreatorLoop: Build Plan Overview

> A creator platform where fans support creators with fiat (**Dodo Payments**) and crypto tips, a graph-powered trust engine (**TigerGraph**) filters out bots, collusion rings and payment fraud before any reward is paid, AI agents running on free models from **AI Grants India (AIGI)** investigate fraud, moderate content and help creators, and **viaSocket** automates every notification, alert and scheduled job. Posts are anchored on Polygon and rewards are claimed on-chain. That Web3 layer is deliberately thin.

This plan replaces the earlier Anon Aadhaar design. Proof of personhood is now **behavioural**: a graph-based trust score, not an identity document.

---

## 1. Sponsor and tech focus

| Tech | Role in CreatorLoop | Why it's central, not decorative |
|---|---|---|
| **TigerGraph** (Savanna cloud or Community Edition) | Trust graph of accounts, devices, IP buckets, wallets, funders, posts and payments. Runs TrustRank, WCC, Louvain and Jaccard to compute a `trust_score` and flag fraud rings. | Every reward and every payout is weighted by trust. Remove TigerGraph and the economy is farmable. |
| **Dodo Payments** | Merchant-of-record checkout for **Supporter Pass** subscriptions (per creator) and **Boost Packs** (one-time). Webhooks drive an internal ledger that funds the creator pool. | It's the platform's revenue engine and the source of the creator fund. |
| **viaSocket** | Workflow automation: payment → creator notification, fraud case → admin alert, cron → epoch run, ledger → Google Sheets, weekly creator digest. Its MCP server is available to the agents. | All ops glue lives in visible, editable flows rather than buried cron code. That makes a strong demo. |
| **AIGI** (AI Grants India free model access) | Powers three agents: **Fraud Investigator** (tool-calls GSQL queries and writes case reports), **Moderator** (caption and image policy checks), **Creator Copilot** (captions, tags; voice via Smallest AI if available). | The agents turn raw graph signals into decisions a human admin can approve in one click. |
| Polygon Amoy + IPFS (light) | Posts anchored as IPFS CIDs; POL tips; LOOP reward token distributed via Merkle claims. | Transparent, verifiable reward payouts, kept to four small contracts. |

## 2. Architecture

```
┌──────────────────────────── apps/web (Next.js) ────────────────────────────┐
│ Privy login + embedded wallet │ Feed / Post / Create / Creator / Earnings   │
│ Dodo checkout redirect        │ Admin: fraud cases, approve/deny, epochs    │
└──────┬──────────────┬─────────────────┬──────────────────────┬─────────────┘
       │ upload       │ REST (JWT)      │ tx (createPost, tip,  │ redirect
       ▼              ▼                 │ claim)                ▼
   Pinata/IPFS   ┌───────────────────── services/api (FastAPI) ─────────┐   Dodo Checkout
                 │ • reactions/follows (off-chain, authed)               │◄── webhooks ── Dodo
                 │ • Dodo webhook → ledger (Postgres)                    │
                 │ • chain indexer (web3.py getLogs) → Postgres          │
                 │ • graph sync → TigerGraph (pyTigerGraph upserts)      │
                 │ • trust job (installed GSQL queries) → trust_score    │
                 │ • scorer → Merkle → RewardDistributor.publishEpoch    │
                 │ • agents (AIGI models) ── tools ── GSQL / DB / viaSocket
                 │ • outbound events (HMAC) ─────────────────────────────┼──► viaSocket flows
                 └──────┬───────────────┬────────────────────────────────┘        │
                        ▼               ▼                                         ▼
                   Postgres        TigerGraph                     Discord/Telegram/Email/Sheets
                (source of truth) (trust engine)                  + cron back into /internal/*
                        ▲
                        │ events
              Polygon Amoy: CreatorPosts · Tipping · LoopToken · RewardDistributor
```

**Core loop:**
1. A fan logs in with Privy. Device and IP signals are recorded (hashed).
2. The fan reacts, follows, tips in POL, or buys a Supporter Pass with Dodo.
3. Everything lands in Postgres and is mirrored to TigerGraph as vertices and edges.
4. The trust job runs graph algorithms and writes `trust_score` plus fraud flags. A suspicious cluster opens a **case**, the Fraud Investigator agent writes a report, viaSocket alerts the admin, and the admin approves or denies.
5. Each epoch (viaSocket cron), the scorer weights engagement by reactor trust, adds the Dodo-funded creator pool, excludes frozen accounts, builds a Merkle tree and publishes the root on-chain.
6. Creators claim LOOP on-chain. Fiat creator-fund balances show in the ledger; payout rails are out of scope (see Stage 07).

## 3. Stages and files

| # | File | Stage | Depends on | Effort (focused h) |
|---|---|---|---|---|
| 01 | [01-foundation-and-tooling.md](01-foundation-and-tooling.md) | Monorepo, accounts, secrets, local infra | — | 4–5 |
| 02 | [02-smart-contracts.md](02-smart-contracts.md) | 4 light contracts + tests | 01 | 6–8 |
| 03 | [03-deploy-to-amoy.md](03-deploy-to-amoy.md) | Ignition deploy, verify, export | 02 | 2–3 |
| 04 | [04-data-backbone.md](04-data-backbone.md) | FastAPI skeleton, Postgres schema, chain indexer, signals capture | 01, 03 | 8–10 |
| 05 | [05-tigergraph-trust-graph.md](05-tigergraph-trust-graph.md) | Graph schema, sync, GSQL algorithms, trust score, cases | 04 | 12–16 |
| 06 | [06-ipfs-media-pipeline.md](06-ipfs-media-pipeline.md) | Upload, metadata JSON, gateways | 01 | 3–5 |
| 07 | [07-dodo-payments.md](07-dodo-payments.md) | Products, checkout, webhooks, ledger, creator fund | 04 | 8–10 |
| 08 | [08-viasocket-automations.md](08-viasocket-automations.md) | Outbound events, 5 flows, cron, MCP | 04, 05, 07 | 5–7 |
| 09 | [09-aigi-ai-agents.md](09-aigi-ai-agents.md) | Model gateway, 3 agents, tools, evals | 05, 08 | 10–14 |
| 10 | [10-frontend-nextjs.md](10-frontend-nextjs.md) | Web app incl. admin console | 03, 04, 06, 07 | 14–18 |
| 11 | [11-scoring-and-reward-epochs.md](11-scoring-and-reward-epochs.md) | Trust-weighted scoring, Merkle, publish, claims API | 05, 07, 03 | 8–10 |
| 12 | [12-testing-security-qa.md](12-testing-security-qa.md) | Threat model, tests, red-team the trust engine | all | ongoing + 6 |
| 13 | [13-demo-pitch-submission.md](13-demo-pitch-submission.md) | Seed + attack simulation, demo script, submission | all | 5–6 |

**Total:** roughly 90–120 focused hours, about right for a team of 3–4 over a 6-day hacker house.

### Dependency graph and tracks

```
01 ─┬─► 02 ─► 03 ─┐
    ├─► 04 ◄──────┘─┬─► 05 ─┬─► 09
    │               ├─► 07 ─┼─► 08
    │               │       └─► 11
    └─► 06 ─────────┴─────────► 10 ─► 12 ─► 13
```

| Track | Owner | Stages |
|---|---|---|
| Chain + backend core | Dev A | 02, 03, 04, 11 |
| Graph + AI | Dev B | 05, 09 |
| Payments + automation | Dev C | 07, 08 |
| Frontend | Dev D (or split A/C) | 06, 10 |

## 4. Six-day schedule

| Day | A (chain/backend) | B (graph/AI) | C (payments/automation) | D (frontend) |
|---|---|---|---|---|
| 1 | 01 together; contracts | 01; TigerGraph instance + schema | 01; Dodo test products | 01; Next.js shell + Privy |
| 2 | 03 deploy; 04 API + DB + indexer | 05 sync loader + first queries | 07 checkout + webhook handler | 06 uploads; create post |
| 3 | 04 signals capture; 11 scorer core | 05 TrustRank/WCC/Louvain → trust score | 07 ledger + creator fund; 08 outbound events | feed, post, tip, react |
| 4 | 11 Merkle + publish; claims API | 09 model gateway + Fraud Investigator | 08 flows (5) live in viaSocket | creator page, support checkout, earnings |
| 5 | 12 contract security | 09 Moderator + Copilot; evals | 12 payment edge cases | admin console (cases, approve) |
| 6 | 13 seed + attack sim | 13 demo case report | 13 demo flows | 13 polish + recording |

## 5. Conventions

- **pnpm** workspaces for JS; **uv** for Python 3.12; **Node ≥ 22.10**.
- **One backend** (`services/api`, FastAPI) owns Postgres, TigerGraph, Dodo webhooks, agents and the scorer. Next.js route handlers are used only for Pinata signed uploads.
- **Postgres is the source of truth; TigerGraph is a derived index.** It can always be rebuilt from Postgres with `make graph-rebuild`.
- **Every external callback is authenticated:** Dodo (Standard Webhooks signature), viaSocket → us (shared-secret header), us → viaSocket (HMAC-signed body).
- **All money is in integer minor units** (paise or cents) in the DB, never floats.
- **Fast-moving SDKs** (Hardhat 3, Privy, Pinata, Dodo, pyTigerGraph, viaSocket, AIGI-provided model APIs): each stage gives the expected shape. Check current docs before coding, and pin versions once working.

## 6. Definition of done

- [ ] Contracts deployed and verified on Amoy; addresses in `@creatorloop/shared`.
- [ ] A fan can buy a Supporter Pass for a creator in Dodo **test mode**. The webhook updates the ledger, and viaSocket notifies the creator within 1 minute.
- [ ] Reactions, follows, tips and payments appear in TigerGraph within 1 minute of happening.
- [ ] The attack simulation (Stage 13) creates a 20-account bot ring. The trust job flags it, opens a case, and the Fraud Investigator agent writes a report. The admin freezes the ring in one click.
- [ ] An epoch run (triggered by viaSocket cron) excludes frozen accounts. The honest creator's share visibly beats the bot-ring creator's, and the claim succeeds on-chain.
- [ ] The Moderator blocks a policy-violating caption; the Copilot suggests captions and tags.
- [ ] Security checks from Stage 12 pass.
- [ ] Demo video, README and submission are done.

## 7. Key decisions

| Decision | Choice | Reason |
|---|---|---|
| Sybil defence | TigerGraph trust score (behavioural, graph-based) instead of identity proofs | No PII collection, works globally, and catches collusion rings, which identity checks cannot |
| Where engagement lives | Off-chain (Postgres) for reactions and follows; on-chain for posts, tips and claims | Spam-proof without a personhood gate, and cheap. The chain is used only where verifiability matters. |
| Money in | Dodo Payments (merchant of record) | Handles tax and compliance, India-friendly, test mode for the hackathon |
| Money out | LOOP via Merkle claims on-chain; fiat creator-fund balances accrued in the ledger | Fiat payouts to third parties need a payout or marketplace product and KYC, which is out of scope. State it clearly in the pitch. |
| Agents | Model-agnostic gateway; AIGI-provided models primary | Free access; the gateway lets you swap providers if quotas run out |
| Ops automation | viaSocket flows, triggered by signed outbound events and cron | Visible, editable, and demoable |
