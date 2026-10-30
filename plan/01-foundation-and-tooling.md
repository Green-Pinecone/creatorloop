# Stage 01: Foundation and Tooling

**Goal:** a monorepo every later stage drops into, all accounts and sandboxes created (Dodo test mode, a TigerGraph instance, a viaSocket workspace, AIGI model access, Pinata, Privy), local infra running, and secrets handled safely from the first commit.

**Prerequisites:** none.
**Outputs:** a git repo skeleton, `docker compose up` bringing up Postgres (plus optionally local TigerGraph), funded Amoy wallets, and every account in the table in §4.

---

## 1. Toolchain

| Tool | Version | Install / check |
|---|---|---|
| Node.js | ≥ 22.10 | `node -v` (Hardhat 3 needs ESM + 22.10 or newer) |
| pnpm | 9 or 10 | `corepack enable && corepack prepare pnpm@latest --activate` |
| Python | 3.12 | `py -3.12 --version` |
| uv | latest | `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Docker Desktop | latest | Postgres locally; TigerGraph Community Edition if not using Savanna |
| Slither | latest | `uv tool install slither-analyzer` (Stage 12) |
| ngrok or cloudflared | latest | Exposes the local API to Dodo and viaSocket webhooks |

> **Windows:** create the repo in a path **without spaces** (for example `C:\dev\creatorloop`). Several tools (Docker volume mounts, some Python wheels, Hardhat cache paths) misbehave with `Hacker House Project`-style paths.

## 2. Repository layout

```
creatorloop/
├─ apps/web/                     # Next.js frontend + Pinata upload routes      (06, 10)
├─ packages/
│  ├─ contracts/                 # Hardhat 3                                    (02, 03)
│  └─ shared/                    # ABIs, addresses, metadata schema (TS)        (03, 06)
├─ services/api/                 # FastAPI: DB, indexer, graph, Dodo, agents, scorer
│  ├─ app/
│  │  ├─ main.py
│  │  ├─ config.py
│  │  ├─ db/ (models.py, session.py, migrations/)
│  │  ├─ routers/ (public.py, me.py, admin.py, webhooks.py, internal.py)
│  │  ├─ chain/ (indexer.py, publisher.py, abis/)
│  │  ├─ graph/ (client.py, sync.py, gsql/, trust.py)
│  │  ├─ payments/ (dodo.py, ledger.py)
│  │  ├─ automations/ (events.py)
│  │  ├─ agents/ (gateway.py, investigator.py, moderator.py, copilot.py, tools.py)
│  │  └─ scoring/ (score.py, merkle.py, epoch.py)
│  ├─ tests/
│  └─ pyproject.toml
├─ infra/
│  ├─ docker-compose.yml
│  └─ viasocket/ (exported flow notes/screenshots)
├─ plan/
├─ .github/workflows/ci.yml
└─ README.md
```

```bash
mkdir creatorloop && cd creatorloop && git init -b main && pnpm init
mkdir -p apps/web packages/contracts packages/shared/src services/api/app infra/viasocket
```

`pnpm-workspace.yaml`:

```yaml
packages: ["apps/*", "packages/*"]
```

## 3. Local infrastructure

`infra/docker-compose.yml`:

```yaml
services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: creatorloop
      POSTGRES_PASSWORD: creatorloop
      POSTGRES_DB: creatorloop
    ports: ["5432:5432"]
    volumes: [pg:/var/lib/postgresql/data]

  # Optional: only if not using TigerGraph Savanna (cloud). Check the current image name/tag
  # and RAM requirements in TigerGraph's Community Edition docs (it needs several GB of RAM).
  # tigergraph:
  #   image: tigergraph/community:<version>
  #   ports: ["14240:14240", "9000:9000"]
  #   ulimits: { nofile: { soft: 1000000, hard: 1000000 } }

volumes: { pg: {} }
```

**TigerGraph choice:**

| Option | Pros | Cons |
|---|---|---|
| **Savanna (cloud)**, recommended | No local RAM pressure; reachable by teammates; GraphStudio UI for the demo | Account and credit setup; workspace may auto-suspend when idle |
| Community Edition (Docker) | Offline, free | Heavy on a laptop (8 GB+ RAM recommended); x86 only |

Pick Savanna unless you're blocked. Whichever you choose, write the host and credentials to `services/api/.env`.

## 4. Accounts and keys

| Service | Create | Secrets to store | Where |
|---|---|---|---|
| Amoy deployer wallet | Fresh EOA | `AMOY_PRIVATE_KEY` | Hardhat keystore |
| Amoy publisher wallet | Fresh EOA (scorer hot key) | `PUBLISHER_PRIVATE_KEY` | `services/api/.env` |
| Amoy RPC | Alchemy or Infura Amoy app (public `https://rpc-amoy.polygon.technology` for dev) | `AMOY_RPC_URL` | keystore + api `.env` + web `.env.local` |
| Etherscan v2 key | For contract verification | `ETHERSCAN_API_KEY` | keystore |
| Faucet | `https://faucet.polygon.technology` (deployer ~1 POL, publisher ~0.5 POL, plus demo wallets) | — | — |
| **Dodo Payments** | Business account → **Test mode**. Create products (Stage 07). Developer → API key + webhook endpoint. | `DODO_PAYMENTS_API_KEY`, `DODO_PAYMENTS_WEBHOOK_SECRET` | api `.env` |
| **TigerGraph** | Savanna workspace (or local CE). Create a graph `CreatorLoop` and a secret/token for REST++. | `TG_HOST`, `TG_GRAPH`, `TG_SECRET` (or `TG_TOKEN`), `TG_USERNAME`/`TG_PASSWORD` for the GSQL admin | api `.env` |
| **viaSocket** | Workspace. One flow per automation (Stage 08); copy each flow's webhook URL. | `VIASOCKET_WEBHOOK_*` URLs, `VIASOCKET_SIGNING_SECRET`, `INTERNAL_API_TOKEN` (viaSocket → us) | api `.env` + viaSocket flow config |
| **AIGI** (AI Grants India) | Apply for free model access at the official AIGI site. Record which providers/models, endpoints and quotas you receive. | `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL_*` (one per agent), optional `SMALLEST_API_KEY` | api `.env` |
| Privy | App; add `http://localhost:3000` and the deploy URL to allowed origins | `NEXT_PUBLIC_PRIVY_APP_ID`, `PRIVY_APP_SECRET`, plus the Privy JWT verification key for the API | web `.env.local`, api `.env` |
| Pinata | JWT (pinning scope) + dedicated gateway | `PINATA_JWT`, `NEXT_PUBLIC_PINATA_GATEWAY` | web `.env.local` |

> **AIGI is the long pole.** Approval and onboarding can take days. Apply on day 0. Stage 09's model gateway is provider-agnostic, so development can start against any OpenAI-compatible endpoint and switch to AIGI-provided models when access lands. Record the exact terms you receive in `plan/aigi-access.md` (private) so the team knows the quotas.

Generate fresh wallets (after viem is installed in `packages/contracts`):

```bash
node -e "const {generatePrivateKey,privateKeyToAccount}=await import('viem/accounts');const k=generatePrivateKey();console.log(k,privateKeyToAccount(k).address)"
```

## 5. Environment templates

`services/api/.env.example`:

```dotenv
# Core
DATABASE_URL=postgresql+psycopg://creatorloop:creatorloop@localhost:5432/creatorloop
PUBLIC_BASE_URL=https://<your-tunnel>.ngrok.app
WEB_BASE_URL=http://localhost:3000
INTERNAL_API_TOKEN=                    # viaSocket → /internal/* calls
SIGNAL_HASH_SALT=                      # salts device/IP hashes (Stage 04)

# Chain
AMOY_RPC_URL=
PUBLISHER_PRIVATE_KEY=
CHAIN_START_BLOCK=

# Privy (verify user JWTs)
PRIVY_APP_ID=
PRIVY_VERIFICATION_KEY=

# TigerGraph
TG_HOST=https://<workspace>.tgcloud.io
TG_GRAPH=CreatorLoop
TG_SECRET=
TG_USERNAME=
TG_PASSWORD=

# Dodo Payments
DODO_PAYMENTS_API_KEY=
DODO_PAYMENTS_WEBHOOK_SECRET=
DODO_ENVIRONMENT=test_mode
DODO_PRODUCT_SUPPORTER_PASS=
DODO_PRODUCT_BOOST_PACK=

# viaSocket
VIASOCKET_SIGNING_SECRET=
VIASOCKET_WEBHOOK_PAYMENT=
VIASOCKET_WEBHOOK_FRAUD=
VIASOCKET_WEBHOOK_LEDGER=
VIASOCKET_WEBHOOK_DIGEST=

# AI (AIGI-provided models via an OpenAI-compatible gateway)
AI_BASE_URL=
AI_API_KEY=
AI_MODEL_INVESTIGATOR=
AI_MODEL_MODERATOR=
AI_MODEL_COPILOT=
SMALLEST_API_KEY=

# Rewards
EPOCH_POOL_LOOP_WEI=10000000000000000000000
CREATOR_FUND_SHARE_BPS=7000
```

`apps/web/.env.example`:

```dotenv
NEXT_PUBLIC_PRIVY_APP_ID=
NEXT_PUBLIC_CHAIN_ID=80002
NEXT_PUBLIC_AMOY_RPC_URL=
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_PINATA_GATEWAY=
PRIVY_APP_SECRET=
PINATA_JWT=
```

`.gitignore` essentials: `node_modules/`, `.env`, `.env.*` (but `!.env.example`), `.next/`, `artifacts/`, `cache/`, `.venv/`, `__pycache__/`.

## 6. Python service bootstrap

```bash
cd services/api
uv init --package --python 3.12
uv add fastapi "uvicorn[standard]" pydantic-settings sqlalchemy[asyncio] psycopg[binary] alembic \
       httpx web3 eth-abi eth-utils pyTigerGraph dodopayments standardwebhooks \
       openai pyjwt[crypto] tenacity structlog apscheduler
uv add --dev pytest pytest-asyncio respx ruff mypy
```

`app/config.py` uses `pydantic-settings` to load and validate every env var at startup, failing fast if one is missing.

## 7. CI

```yaml
name: ci
on: [push, pull_request]
jobs:
  contracts:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: pnpm }
      - run: pnpm install --frozen-lockfile
      - run: pnpm --filter @creatorloop/contracts test
  api:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17
        env: { POSTGRES_USER: t, POSTGRES_PASSWORD: t, POSTGRES_DB: t }
        ports: ["5432:5432"]
    defaults: { run: { working-directory: services/api } }
    env: { DATABASE_URL: postgresql+psycopg://t:t@localhost:5432/t }
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync && uv run ruff check . && uv run pytest -q
```

TigerGraph, Dodo, viaSocket and AI calls are **mocked in CI** (respx / fakes). Live integration tests run manually via `pytest -m live`.

## 8. Acceptance checklist

- [ ] `pnpm install` and `uv sync` succeed on every teammate's machine.
- [ ] `docker compose -f infra/docker-compose.yml up -d` brings up Postgres; `psql` connects.
- [ ] TigerGraph is reachable: `curl $TG_HOST/restpp/echo` (with a token) returns OK, or GraphStudio opens.
- [ ] Dodo test-mode API key works (list products with the SDK).
- [ ] A viaSocket test flow receives a webhook from `curl`.
- [ ] An AI endpoint answers a hello-world chat completion (AIGI, or a temporary dev provider).
- [ ] Amoy wallets are funded.
- [ ] `git status` shows no env files staged.

## 9. Pitfalls

- **Tunnels change URLs** on free ngrok plans. Use a reserved domain or Cloudflare Tunnel, or you'll keep re-pasting webhook URLs into Dodo and viaSocket.
- **Savanna idle suspend:** the first query after idle can be slow or fail. Wake the workspace before demos.
- **Quotas:** free model access and free automation tiers have limits. Log usage from day 1 (Stage 09 gateway).
