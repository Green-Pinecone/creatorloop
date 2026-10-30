# Stage 10: Frontend (Next.js)

**Goal:** a fast, mobile-first web app covering three audiences:
- **fans:** feed, react, follow, tip, Supporter Pass,
- **creators:** create with Copilot, creator page, earnings and claims,
- **admins:** trust cases, agent reports, graph view, epochs.

It talks to the FastAPI backend for data, to Pinata (via its own route handlers) for uploads, to Dodo checkout via redirects, and to Polygon only for `createPost`, `tip` and `claim`.

**Prerequisites:** Stage 03 (ABIs/addresses in `@creatorloop/shared`), Stage 04 (API), Stage 06 (upload routes), Stage 07 (checkout endpoints). Stage 09 for Copilot and reports.
**Outputs:** `apps/web` deployed (Vercel or similar) with all pages below and the E2E smoke tests passing.

---

## 1. Stack

| Concern | Choice |
|---|---|
| Framework | Next.js (App Router) + TypeScript + Tailwind + shadcn/ui |
| Auth + wallet | `@privy-io/react-auth` (email/Google/wallet login, embedded wallet) + `@privy-io/wagmi` |
| Chain | wagmi + viem, Amoy only |
| Data | TanStack Query against the FastAPI REST endpoints; a typed client generated from FastAPI's OpenAPI (`openapi-typescript`) |
| Forms | react-hook-form + zod (shared schemas) |
| Graph viz (admin) | `react-force-graph-2d` or Cytoscape.js for case neighbourhoods |
| Charts (earnings/admin) | Recharts |
| E2E | Playwright |

```bash
pnpm create next-app apps/web --ts --tailwind --app --src-dir --eslint
cd apps/web
pnpm add @privy-io/react-auth @privy-io/wagmi wagmi viem @tanstack/react-query zod react-hook-form \
         pinata browser-image-compression react-force-graph-2d recharts openapi-fetch
pnpm add -D openapi-typescript @playwright/test
pnpm dlx shadcn@latest init
```

`pnpm openapi-typescript http://localhost:8000/openapi.json -o src/lib/api-types.ts` generates the API types. Re-run it whenever the API changes; CI fails if they drift.

## 2. Providers

`src/app/providers.tsx`:

```tsx
"use client";
import { PrivyProvider } from "@privy-io/react-auth";
import { WagmiProvider, createConfig } from "@privy-io/wagmi";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { polygonAmoy } from "viem/chains";
import { http } from "viem";

const wagmiConfig = createConfig({
  chains: [polygonAmoy],
  transports: { [polygonAmoy.id]: http(process.env.NEXT_PUBLIC_AMOY_RPC_URL) },
});
const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 15_000 } } });

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <PrivyProvider
      appId={process.env.NEXT_PUBLIC_PRIVY_APP_ID!}
      config={{
        loginMethods: ["email", "google", "wallet"],
        embeddedWallets: { ethereum: { createOnLogin: "users-without-wallets" } },
        defaultChain: polygonAmoy,
        supportedChains: [polygonAmoy],
      }}
    >
      <QueryClientProvider client={queryClient}>
        <WagmiProvider config={wagmiConfig}>{children}</WagmiProvider>
      </QueryClientProvider>
    </PrivyProvider>
  );
}
```

> Check Privy's current config keys (`embeddedWallets` shape, `createOnLogin` values) and whether gas sponsorship is available on Amoy. Sponsored gas removes the "you need POL" hurdle for new fans.

## 3. API client with auth and device signals

`src/lib/api.ts`:

```ts
import createClient from "openapi-fetch";
import type { paths } from "./api-types";

export function makeApi(getToken: () => Promise<string | null>) {
  const client = createClient<paths>({ baseUrl: process.env.NEXT_PUBLIC_API_URL });
  client.use({
    async onRequest({ request }) {
      const t = await getToken();
      if (t) request.headers.set("Authorization", `Bearer ${t}`);
      return request;
    },
  });
  return client;
}

export function deviceId(): string {
  try {
    let id = localStorage.getItem("cl_device");
    if (!id) { id = crypto.randomUUID(); localStorage.setItem("cl_device", id); }
    return id;
  } catch { return "no-storage"; }
}
```

After login, call `POST /me/session` once per app load with `{device_id, tz, screen, ua}` (Stage 04), then `POST /me/wallet` (a signed nonce) if the wallet isn't linked yet.

## 4. Pages

| Route | Audience | Content and actions |
|---|---|---|
| `/` | All | Feed: infinite scroll (keyset cursor), boosted posts pinned with a "Boosted" label, react ❤️ (optimistic), tip button, creator chip with supporter badge |
| `/p/[postId]` | All | Post detail, tip history, react, share |
| `/c/[handle]` | All | Creator page: bio, posts, supporter count, **Become a Supporter** (Dodo checkout), follow |
| `/create` | Creators | Upload (Stage 06) → **Copilot suggestions** (captions + tags; voice caption stretch) → `createPost` tx → "indexing…" → done |
| `/me` | Logged in | Profile, trust tier badge (coarse: Trusted / Normal / Limited / Under review), linked wallet, subscriptions |
| `/me/earnings` | Creators | LOOP claimable per epoch → **Claim** tx; creator-fund balance (₹, available vs. held), recent supporter payments, chart |
| `/admin` | Admins | Case queue sorted by severity; filters by kind/status |
| `/admin/cases/[id]` | Admins | Agent report with expandable evidence (tool outputs), **graph view** of the neighbourhood (members red, linked signals grey, creators blue), member table with trust parts; **Freeze / Restrict / Dismiss** with a note |
| `/admin/epochs` | Admins | Epoch history, totals, excluded accounts, allocations CID link, publish tx link |
| `/admin/moderation` | Admins | Flagged posts queue: approve or remove (remove → API → `setHidden` + unpin) |

### Key interaction details

**React (off-chain):** optimistic toggle → `POST /me/reactions/{id}` → roll back on error. Show a gentle toast on 403 ("Your account is under review").

**Tip (on-chain):**

```tsx
const { writeContractAsync } = useWriteContract();
await writeContractAsync({
  address: amoy.Tipping, abi: tippingAbi, functionName: "tip",
  args: [BigInt(postId)], value: parseEther(amount),
});
```

Then wait for the receipt and poll `GET /posts/{id}` until the tip appears (the indexer runs about 5 confirmations behind, roughly 10–15 s). Show "Confirming on Polygon…" and then "Indexed ✓".

**Supporter Pass (Dodo):** `POST /checkout/supporter-pass` → `window.location = checkout_url`. On return (`?checkout=done`), show "Processing payment…" and poll `GET /me/subscriptions` every 2 s for up to 60 s. Never treat the redirect as success.

**Claim (on-chain):** `GET /me/earnings` returns `[{epochId, amount, proof}]`. Each row has a Claim button that calls `RewardDistributor.claim(epochId, amount, proof)`. Disable it and show the reason if the account is frozen (the API also returns `frozen: true`).

**Create:** a step indicator (Upload → Caption → Publish → Indexed) with retry at every step. Store the draft in `sessionStorage` so a failed tx doesn't lose the caption.

## 5. Admin graph view

`GET /admin/cases/{id}` returns `neighbourhood: {nodes:[{id,type,label,trust}], links:[{source,target,type}]}` from the GSQL `neighbourhood` query (Stage 05).

```tsx
<ForceGraph2D
  graphData={data.neighbourhood}
  nodeLabel={(n) => `${n.type}: ${n.label}${n.trust != null ? ` (trust ${n.trust.toFixed(2)})` : ""}`}
  nodeColor={(n) => n.type === "Account" ? (n.trust < 0.2 ? "#e5484d" : n.trust < 0.4 ? "#f5a524" : "#3b82f6") : "#9ca3af"}
  linkLabel={(l) => l.type}
/>
```

This screen is the demo's centrepiece: a ring lights up red, and the agent report sits beside it.

## 6. UX and quality bar

- Mobile-first: the feed and post views work at 360 px wide.
- Every async action has idle, pending, success and error states, and no silent failures.
- Text from captions and bios renders as plain text (no `dangerouslySetInnerHTML`).
- Money formatting: `Intl.NumberFormat("en-IN", { style: "currency", currency })` from minor units.
- Accessibility: buttons have labels; colour isn't the only signal in the graph legend.
- Empty states: new creator, no posts, no earnings yet, no cases.

## 7. Tests

- **Component:** the feed card renders hostile captions as text; the money formatter handles paise.
- **Playwright E2E**, against a local stack (Hardhat node + API + Postgres; TigerGraph and the agents mocked):
  1. Login (Privy test mode or a mocked auth provider) → create post → appears in feed.
  2. A second user reacts → count increments.
  3. Supporter checkout → mock webhook → badge appears.
  4. Admin opens a seeded case → clicks Freeze → member status updates.
  5. Claim flow with a pre-published epoch.

## 8. Acceptance checklist

- [ ] All routes implemented, including admin.
- [ ] A cold user goes from login to first reaction in under 30 s.
- [ ] The tip and claim txs work on Amoy from a Privy embedded wallet.
- [ ] Supporter checkout round-trip works in Dodo test mode.
- [ ] The admin case view shows the graph and the agent report; a decision applies.
- [ ] Lighthouse mobile performance ≥ 80 on `/`.
- [ ] E2E suite green.

## 9. Pitfalls

- **Embedded wallet with no POL:** a new fan can't tip. Fund demo wallets (Stage 13), or enable gas sponsorship if available.
- **Hydration issues** with Privy and wagmi: keep wallet-dependent UI in client components and render a skeleton until `ready`.
- **CORS:** the API must allow the web origin(s) only, with credentials off (bearer tokens).
- **Stale API types:** regenerate after every backend change, enforced in CI.
