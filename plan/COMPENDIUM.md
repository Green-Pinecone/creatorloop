# Hacker House Goa 2024 — Tech Compendium & Draft Project

## 1. Method and caveats

- **Source set:** all 102 projects from the Hacker House Goa 2024 Devfolio gallery (queried via Devfolio's public search API; 91 list a GitHub repo, 92 distinct repos resolved, 11 were deleted or private).
- **"Winners":** Devfolio never published winners for this event (every prize's `projects` list is empty) and no repo description mentions a track win. This compendium therefore cannot claim winner status for anything.
- **"Most starred":** star counts are tiny (max 3 across all repos), so rank is weak evidence. The 10 top-starred repos were shallow-cloned to a temporary folder, analyzed statically, and then deleted. Nothing was installed or executed.
- **Analysis depth:** re-lite `js_analyze` on 5 repos (ownsound, Shopsy-Main, cra-nillion, crypbucket, on-chain-meme). The other 5 (EtherScore-Defi, de-code, SwitchSocial, revee, MemeCore) were read from README, dependency files and file layout only. SwitchSocial contains only a backend folder, so it yielded little.
- **Stronger signal:** the tech-tag counts over all 102 projects (section 3) matter more than the 10-repo deep dive.

## 2. Repos analyzed

| Repo | Stars | Core tech found |
|---|---|---|
| Shopsy-Main | 3 | React + Vite, Express, Mongoose/MongoDB, web3.js (a conventional app with light Web3) |
| crypbucket | 2 | React (CRA), Tailwind, Garden Finance SDK (`@gardenfi/*`), ethers, bitcoinjs-lib, zustand, CoinGecko, BlockCypher |
| ownsound | 2 | Next.js, Privy auth, ethers, Polygon Amoy, Solidity contracts, The Graph subgraph, IPFS via Pinata, Express backend, Inco FHE (`fhevmjs`) |
| EtherScore-Defi | 2 | FastAPI, React + Vite, ML data collection and training, explainable on-chain credit score |
| on-chain-meme | 2 | React + Vite, wagmi, RainbowKit, viem, ethers, Solidity, The Graph subgraph + urql, Pinata IPFS, Core DAO testnet |
| cra-nillion | 1 | React, MUI, Tailwind, `@nillion/client-web`, Python Nada programs (blind compute / blind inference) |
| de-code | 1 | Web app + Android app, token-rewarded coding quizzes, hackathon sponsorship, tutoring |
| revee | 1 | React + Vite, Anon Aadhaar (`@anon-aadhaar/react`), Firebase, Solidity |
| MemeCore | 1 | Next.js, next-auth, Hardhat, ethers, IPFS (Lighthouse), Gemini, Firebase, Airtable, tipping |
| SwitchSocial | 1 | Backend only, little to analyze |

## 3. Frequency across all 102 projects (Devfolio tags)

React/Next.js ≈ 80 (reactjs 39, nextjs 25, react 18) · Solidity 38 · Python 30 · TypeScript 21 · Tailwind 19 · Node.js/Express ≈ 31 · Polygon 17 · Hardhat 12 · **The Graph 12** · MetaMask 10 · **Nillion 10** · **Anon Aadhaar 16** (two spellings) · IPFS 8 · MongoDB 8 · Diamante 8 · Core DAO 8 · Move/Aptos 11 · Garden Finance 6.

Most-targeted prize tracks: Anon Aadhaar SDK (31 projects), Nillion Blind App (30), Polygon Amoy/PoS dApp (27), Diamante (18), MusicX (15), Subgraph/Substream (25 across two tracks), Garden Finance SDK (15), SocialFi (10).

## 4. What repeats (the architectural pattern)

1. **Frontend:** React/Next.js + TypeScript + Tailwind.
2. **Wallet and auth:** MetaMask, RainbowKit/wagmi or Privy, with ethers/viem to call contracts.
3. **Contracts:** Solidity + Hardhat, deployed to a cheap EVM testnet (Polygon Amoy was the favorite).
4. **Indexing:** The Graph subgraphs to query on-chain events (ownsound, on-chain-meme), consumed with urql or GraphQL.
5. **Storage:** media on IPFS through Pinata or Lighthouse, with only the CID on-chain.
6. **Sybil and identity:** Anon Aadhaar zero-knowledge proof of personhood (the single most-targeted sponsor SDK).
7. **Theme:** SocialFi and creator economy: memes, music, tipping, token rewards for engagement (ownsound, on-chain-meme, MemeCore, SwitchSocial, de-code).
8. **Python side:** ML or privacy compute (EtherScore, Nillion Nada), typically behind FastAPI or Flask.

## 5. Draft project: **CreatorLoop** (SocialFi on the repeated stack)

**Pitch:** a creator platform where only verified-unique humans can earn, content lives on IPFS, and engagement pays out on-chain, which fixes the "bots farm rewards" problem the SocialFi entries ignored.

**Architecture**

| Layer | Choice | Why (evidence) |
|---|---|---|
| Web app | Next.js + TypeScript + Tailwind | Top frontend in the dataset |
| Auth and wallet | Privy (embedded wallet) + wagmi/viem | Used in ownsound, on-chain-meme |
| Contracts | Solidity + Hardhat on Polygon Amoy: `CreatorPost` (CID + author), `Tipping`, `RewardPool` | Solidity, Hardhat and Polygon are all top-5 tags |
| Identity | Anon Aadhaar proof gates `claimReward()` | Most-targeted SDK; no SocialFi entry used it for sybil resistance |
| Indexing | The Graph subgraph (`PostCreated`, `Tipped`, `RewardClaimed`) + urql | ownsound and on-chain-meme pattern |
| Storage | Pinata/IPFS for media; CID in event logs | ownsound, on-chain-meme, MemeCore |
| Off-chain service | FastAPI: spam/quality scoring, optional AI caption suggestions | Python is the #3 tag; MemeCore uses AI suggestions |
| Optional stretch | Nillion blind compute for private engagement analytics | 10 projects; strong sponsor-prize fit |

**Build order**
1. Contracts and tests (Hardhat), deploy to Amoy.
2. Subgraph for the three events.
3. Next.js feed + upload flow (IPFS, then `createPost`).
4. Tipping and the reward pool.
5. Anon Aadhaar gate on `claimReward`.
6. FastAPI scoring service, then the Nillion stretch.

**Risks:** Anon Aadhaar requires Indian Aadhaar QR input, which limits who can claim. The Graph Studio endpoints in the analyzed repos are test deployments. Verify SDK versions before reuse.

## 6. Clean-up

The temporary clones in the session scratchpad were deleted after analysis. The raw project list was kept in the scratchpad (`projects.json`, `slim.json`, `stars.tsv`), not in this folder.
