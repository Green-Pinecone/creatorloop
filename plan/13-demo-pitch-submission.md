# Stage 13: Demo, Pitch and Submission

**Goal:** a reproducible demo world, a 3-minute live demo that shows each sponsor technology doing real work, a backup recording, and a complete submission (Devfolio or equivalent) mapped to the relevant sponsor tracks.

**Prerequisites:** everything. Stage 12's pre-demo QA script.
**Outputs:** `scripts/seed/` (demo world + attack simulation), `scripts/demo_reset.sh`, the demo video, the pitch deck, the README, and the submission.

---

## 1. Demo world seeding

`scripts/seed/world.py` (idempotent; uses the API with test auth plus Hardhat scripts for on-chain actions):

| Group | Count | Behaviour |
|---|---|---|
| Honest creators | 4 (`@anjunasunsets`, `@codewithriya`, `@goanbeats`, `@chaiandcharts`) | 3–6 posts each with real images |
| Honest fans | 30 | Distinct devices and networks; organic reactions (Zipf-distributed), a few tips, 6 Supporter Passes (Dodo test mode) |
| Bot-ring creator | 1 (`@totallyrealcreator`) | 3 low-effort posts |
| Device farm (R1) | 12 | 2 devices; spam-react to the ring creator |
| Collusion ring (R2) | 8 | Mutual engagement with the ring creator |
| Funded sockpuppets (R3) | 6 | Funded by one wallet; tip the ring creator |
| Self-dealer (R4) | 1 | The ring creator's alt buys their Supporter Pass |
| Venue Wi-Fi (R7) | 15 honest fans | Share one /24 bucket. Proves no false positives. |

On-chain prerequisites:
- Fund demo wallets from a dedicated **demo funder** address. Add that funder to the supernode allowlist, or *all* demo users will be linked by it.
- Pre-run one epoch **before** the attack, so the "before vs. after" numbers exist.

`scripts/demo_reset.sh`:
1. Truncate off-chain tables (keep chain-derived ones).
2. Rebuild the graph.
3. Re-seed the off-chain world.
4. Run the trust job.
5. Pre-generate agent reports.

It should take under 5 min.

## 2. The three-minute demo script

| Time | Beat | What's on screen | Tech showcased |
|---|---|---|---|
| 0:00 | **Problem:** "Creator rewards get farmed by bots. Identity checks exclude people and leak PII." | One slide | — |
| 0:20 | A fan logs in with Google, sees the feed, reacts, tips 0.01 POL | Feed → tip tx → "Indexed ✓" | Privy, Polygon |
| 0:40 | The fan buys a **Supporter Pass** for `@goanbeats` | Dodo test checkout → back to the app → badge. Creator's phone or email shows the **viaSocket** notification. | **Dodo Payments**, **viaSocket** F1 |
| 1:05 | A creator posts with **Copilot** (optional: Hindi voice → captions) | Create → AI captions → publish | **AIGI** models (+ Smallest AI voice) |
| 1:30 | "Now let's attack it." Run the attack sim live (pre-staged script, about 10 s) | Terminal → counters jump on `@totallyrealcreator` | — |
| 1:45 | The trust job runs (viaSocket cron or a manual button) → **a case opens** → the admin Discord pings with the **agent's summary** | Discord alert | **TigerGraph**, **viaSocket** F2, **AIGI** Investigator |
| 2:05 | Admin console: the graph view lights up the ring in red; the agent report with evidence is beside it; click **Freeze** | Case page | **TigerGraph** viz, agent grounding |
| 2:25 | Epoch runs: the honest creator gets the big share; the ring creator gets about 0 (show the "without trust weighting" number beside it) | `/admin/epochs` + Polygonscan publish tx | Scoring, Merkle, Polygon |
| 2:45 | The creator claims LOOP; the Sheets ledger mirror updates | Claim tx → Sheet row | Polygon, **viaSocket** F4 |
| 2:55 | Close: "Behavioural trust, real-money support, transparent payouts." | Slide | — |

Rules:
- **Record a full backup video** the night before and keep it open in a tab.
- Pre-login all demo accounts in separate browser profiles.
- Never type secrets or show `.env` on screen.

## 3. Pitch deck (8–10 slides)

1. Title + one-liner.
2. Problem: farmed creator rewards; why identity KYC isn't the answer.
3. Solution: the behavioural trust graph plus real-money support.
4. Architecture (Stage 00 diagram, simplified).
5. **TigerGraph:** the graph model + the 4 algorithms + a screenshot of a detected ring.
6. **AIGI agents:** the investigator's grounded report; eval results table (Stage 09).
7. **Dodo Payments:** Supporter Pass → ledger → creator fund; risk signals from payments.
8. **viaSocket:** the six flows (screenshot grid); ops without code.
9. Results: the red-team table (Stage 12), the before/after allocations, latency numbers.
10. Roadmap: fiat creator payouts, mainnet, mobile app, a creator API; team slide.

## 4. README (repo root)

Sections:
- One-liner + demo video link.
- Architecture diagram.
- **Sponsor tech usage**: one subsection each for TigerGraph, Dodo Payments, viaSocket and AIGI, each with *what*, *where in code* (file links) and *why it matters*.
- Deployed contract addresses (Polygonscan links).
- Quickstart: `docker compose up`, env setup, `make seed`, `pnpm web:dev`, `uv run uvicorn …`.
- Red-team results + eval results.
- Limitations, stated honestly: test mode payments; creator-fund payouts not live; trust false positives possible, hence human-in-the-loop; testnet token.
- License.

## 5. Submission checklist

- [ ] Project name, tagline, and the problem and solution text (reuse the README).
- [ ] Demo video (≤ 3 min) uploaded unlisted on YouTube or Loom.
- [ ] Repo public, with the README complete and secrets scanned (`gitleaks`).
- [ ] Live demo URL (web) with seeded data; the admin demo login shared privately with the judges if requested.
- [ ] **Tracks / bounties:** read the current event's sponsor list and prize descriptions, and apply to each one where the usage is genuine:
  - TigerGraph: graph-based fraud and trust, with GSQL queries + algorithms.
  - Dodo Payments: real checkout + webhooks + subscriptions in test mode.
  - viaSocket: production-style automations + MCP.
  - AI Grants India: AI agents built on AIGI-provided model access (cite which models).
  - Polygon: if a track exists.
- [ ] For each track, write a 3–4 line "how we used X" note with file links. Judges skim.
- [ ] Team members, roles and contact details added.
- [ ] Submit **before** the deadline buffer (aim for 2 h early); confirm the submission page renders the video.

## 6. Acceptance checklist

- [ ] `demo_reset.sh` brings a fresh world up in under 5 min, tested twice.
- [ ] The live demo was rehearsed 3× within 3:00, at least once on the venue network.
- [ ] Backup video recorded.
- [ ] Deck, README and submission done; every sponsor section links to code.

## 7. Pitfalls

- **Venue network:** WebSockets or RPC may be blocked; have a phone hotspot ready.
- **Quota or cold start on demo day:** pre-warm TigerGraph, cache the agent outputs, and confirm Dodo test mode and the viaSocket flows the morning of.
- **Overclaiming:** don't call test-mode payments "revenue" or LOOP "money". Judges respect honest scoping.
