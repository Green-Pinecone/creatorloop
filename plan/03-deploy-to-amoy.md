# Stage 03: Deploy to Polygon Amoy

**Goal:** a single-command, resumable deployment of the four contracts, with verified source and exported addresses and ABIs for the web app (TypeScript) and the API (Python).

**Prerequisites:** Stage 02 green. The deployer holds about 1 POL.
**Outputs:** `ignition/deployments/amoy/` committed; `packages/shared/src/deployments/amoy.json` + `abis.ts`; `services/api/app/chain/abis/*.json` + `deployments.json`.

---

## 1. Network facts

| Field | Value |
|---|---|
| Chain ID | `80002` |
| Native token | POL (testnet) |
| Public RPC | `https://rpc-amoy.polygon.technology` (use Alchemy or Infura for the demo) |
| Explorer | `https://amoy.polygonscan.com` |
| Faucet | `https://faucet.polygon.technology` |

## 2. Ignition module

`ignition/modules/CreatorLoop.ts`:

```ts
import { buildModule } from "@nomicfoundation/hardhat-ignition/modules";

export default buildModule("CreatorLoop", (m) => {
  const deployer = m.getAccount(0);
  const publisher = m.getParameter<string>("publisher");
  const enforcer = m.getParameter<string>("enforcer");
  const supply = m.getParameter<bigint>("tokenSupply", 100_000_000n * 10n ** 18n);
  const funding = m.getParameter<bigint>("distributorFunding", 1_000_000n * 10n ** 18n);
  const maxEpochTotal = m.getParameter<bigint>("maxEpochTotal", 100_000n * 10n ** 18n);

  const posts = m.contract("CreatorPosts", [deployer]);
  const tipping = m.contract("Tipping", [posts]);
  const token = m.contract("LoopToken", [deployer, supply]);
  const distributor = m.contract("RewardDistributor", [token, deployer, publisher, enforcer, maxEpochTotal]);
  m.call(token, "transfer", [distributor, funding]);

  return { posts, tipping, token, distributor };
});
```

`ignition/parameters/amoy.json`:

```json
{
  "CreatorLoop": {
    "publisher": "0x<publisher address>",
    "enforcer": "0x<enforcer address (may equal publisher for the hackathon)>"
  }
}
```

## 3. Deploy

```bash
cd packages/contracts
# 1) record the current block for the API indexer's start block
curl -s -X POST -H 'content-type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' https://rpc-amoy.polygon.technology
# 2) deploy + verify
pnpm hardhat ignition deploy ignition/modules/CreatorLoop.ts \
  --network amoy --parameters ignition/parameters/amoy.json \
  --deployment-id amoy --verify
```

- If verification lags, run `pnpm hardhat ignition verify amoy` later.
- If a tx stalls, re-run the same deploy command; Ignition resumes.
- **Never delete** `ignition/deployments/amoy/` to retry.

## 4. Post-deploy assertions (`scripts/check-deployment.ts`)

- [ ] `Tipping.posts()` is `CreatorPosts`.
- [ ] `CreatorPosts.owner()` is the deployer.
- [ ] `RewardDistributor.token()` is `LoopToken`.
- [ ] `hasRole(PUBLISHER_ROLE, publisher)` and `hasRole(ENFORCER_ROLE, enforcer)` are both true.
- [ ] `LoopToken.balanceOf(distributor)` equals the funding amount.
- [ ] All four contracts show as verified on Polygonscan.

## 5. Export for the TypeScript and Python consumers

`scripts/export-deployments.ts`:

```ts
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import path from "node:path";

const id = "amoy";
const startBlock = Number(process.argv[2]);
const root = path.resolve(import.meta.dirname, "..");
const dep = path.join(root, "ignition/deployments", id);
const addrs = JSON.parse(readFileSync(path.join(dep, "deployed_addresses.json"), "utf8"));
const names = ["CreatorPosts", "Tipping", "LoopToken", "RewardDistributor"] as const;
const abi = (n: string) => JSON.parse(readFileSync(path.join(dep, `artifacts/CreatorLoop#${n}.json`), "utf8")).abi;

const deployment = { chainId: 80002, startBlock, ...Object.fromEntries(names.map((n) => [n, addrs[`CreatorLoop#${n}`]])) };

// TypeScript consumers (web)
const shared = path.resolve(root, "../shared/src");
mkdirSync(path.join(shared, "deployments"), { recursive: true });
writeFileSync(path.join(shared, "deployments/amoy.json"), JSON.stringify(deployment, null, 2));
writeFileSync(path.join(shared, "abis.ts"),
  names.map((n) => `export const ${n[0].toLowerCase() + n.slice(1)}Abi = ${JSON.stringify(abi(n))} as const;`).join("\n\n") + "\n");

// Python consumer (API)
const py = path.resolve(root, "../../services/api/app/chain/abis");
mkdirSync(py, { recursive: true });
for (const n of names) writeFileSync(path.join(py, `${n}.json`), JSON.stringify(abi(n), null, 2));
writeFileSync(path.join(py, "deployments.json"), JSON.stringify(deployment, null, 2));
console.log(deployment);
```

Run it with `node --experimental-strip-types scripts/export-deployments.ts <startBlock>` (or `tsx`).

## 6. Acceptance checklist

- [ ] One command deploys everything; re-running it is a no-op.
- [ ] `check-deployment.ts` passes.
- [ ] Both export targets are written; the web app type-checks against `abis.ts`.
- [ ] The README lists addresses with Polygonscan links.

## 7. Pitfalls

- **Publisher = admin:** don't. Keep the hot keys (publisher, enforcer) separate from the admin.
- **Redeploys** change addresses and the start block. Re-run the export, then restart the API indexer from the new start block (Stage 04 handles a clean re-index).
- **Gas spikes on Amoy:** set `maxFeePerGas` in the network config if txs hang.
