# Stage 02: Smart Contracts (the light Web3 layer)

**Goal:** four small, well-tested contracts that do only what needs on-chain verifiability:
- anchoring posts (IPFS CID + author),
- POL tips,
- the LOOP reward token,
- Merkle-based reward claims.

Everything else (reactions, follows, trust, payments) lives off-chain in Postgres and TigerGraph.

**Prerequisites:** Stage 01.
**Outputs:** `packages/contracts` on Hardhat 3, tests passing, coverage of at least 90%.

---

## 1. Contract map

| Contract | Responsibility | Gating |
|---|---|---|
| `CreatorPosts` | `createPost(metadataCid)` emits the CID; owner can hide posts; per-wallet daily cap | Open to any wallet; the daily cap limits spam |
| `Tipping` | Forwards native POL to the post author; emits `Tipped` | Open |
| `LoopToken` | ERC-20 `LOOP`; fixed supply minted to the treasury | — |
| `RewardDistributor` | Per-epoch Merkle roots; `claim(epoch, amount, proof)`; `PUBLISHER_ROLE` for the scorer; admin can **freeze** accounts | Freeze list mirrors TigerGraph enforcement |

**Why there's no identity gate on-chain:** the trust decision happens **before** the Merkle root is built. Frozen or low-trust accounts get zero allocation (Stage 11). The on-chain `frozen` mapping is a backstop for accounts caught *after* a root was published.

## 2. Setup

```bash
cd packages/contracts
pnpm dlx hardhat --init          # Hardhat 3, TypeScript, node:test + viem
pnpm add -D @openzeppelin/contracts@^5 @openzeppelin/merkle-tree
```

`hardhat.config.ts`:

```ts
import { configVariable, defineConfig } from "hardhat/config";
import hardhatToolboxViem from "@nomicfoundation/hardhat-toolbox-viem";

export default defineConfig({
  plugins: [hardhatToolboxViem],
  solidity: { version: "0.8.28", settings: { optimizer: { enabled: true, runs: 200 } } },
  networks: {
    amoy: {
      type: "http",
      chainType: "l1",
      url: configVariable("AMOY_RPC_URL"),
      accounts: [configVariable("AMOY_PRIVATE_KEY")],
    },
  },
  verify: { etherscan: { apiKey: configVariable("ETHERSCAN_API_KEY") } },
});
```

```bash
pnpm hardhat keystore set AMOY_RPC_URL
pnpm hardhat keystore set AMOY_PRIVATE_KEY
pnpm hardhat keystore set ETHERSCAN_API_KEY
```

> Confirm the config keys against the current Hardhat 3 docs. The toolbox and verify plugins were still evolving in 2025–26.

## 3. Contracts

### 3.1 `contracts/interfaces/ICreatorPosts.sol`

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

interface ICreatorPosts {
    function authorOf(uint256 postId) external view returns (address);
}
```

### 3.2 `contracts/CreatorPosts.sol`

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ICreatorPosts} from "./interfaces/ICreatorPosts.sol";

/// @notice Anchors creator posts (IPFS metadata CIDs) on-chain.
contract CreatorPosts is ICreatorPosts, Ownable2Step {
    struct Post {
        address author;
        uint64 createdAt;
        bool hidden;
    }

    uint256 public constant MAX_CID_LENGTH = 100;
    uint256 public postCount;
    uint256 public maxPostsPerDay = 20;

    mapping(uint256 => Post) public posts;
    mapping(address => mapping(uint256 => uint256)) public postsOnDay;

    event PostCreated(uint256 indexed postId, address indexed author, string metadataCid);
    event PostHidden(uint256 indexed postId, bool hidden);
    event MaxPostsPerDayUpdated(uint256 value);

    error BadCid();
    error UnknownPost();
    error DailyLimitReached();

    constructor(address owner_) Ownable(owner_) {}

    function createPost(string calldata metadataCid) external returns (uint256 postId) {
        uint256 len = bytes(metadataCid).length;
        if (len == 0 || len > MAX_CID_LENGTH) revert BadCid();
        uint256 day = block.timestamp / 1 days;
        if (++postsOnDay[msg.sender][day] > maxPostsPerDay) revert DailyLimitReached();

        postId = ++postCount;
        posts[postId] = Post({author: msg.sender, createdAt: uint64(block.timestamp), hidden: false});
        emit PostCreated(postId, msg.sender, metadataCid);
    }

    function setHidden(uint256 postId, bool hidden) external onlyOwner {
        if (posts[postId].author == address(0)) revert UnknownPost();
        posts[postId].hidden = hidden;
        emit PostHidden(postId, hidden);
    }

    function setMaxPostsPerDay(uint256 value) external onlyOwner {
        maxPostsPerDay = value;
        emit MaxPostsPerDayUpdated(value);
    }

    function authorOf(uint256 postId) external view returns (address) {
        return posts[postId].author;
    }
}
```

The owner (a moderation multisig or admin key) hides posts when the Moderator agent plus a human decide to (Stage 09). The API mirrors `hidden` from events.

### 3.3 `contracts/Tipping.sol`

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ICreatorPosts} from "./interfaces/ICreatorPosts.sol";

contract Tipping is ReentrancyGuard {
    ICreatorPosts public immutable posts;
    uint256 public constant MIN_TIP = 0.001 ether;

    event Tipped(uint256 indexed postId, address indexed from, address indexed to, uint256 amount);

    error TipTooSmall();
    error UnknownPost();
    error SelfTip();
    error TransferFailed();

    constructor(ICreatorPosts posts_) { posts = posts_; }

    function tip(uint256 postId) external payable nonReentrant {
        if (msg.value < MIN_TIP) revert TipTooSmall();
        address author = posts.authorOf(postId);
        if (author == address(0)) revert UnknownPost();
        if (author == msg.sender) revert SelfTip();
        emit Tipped(postId, msg.sender, author, msg.value);
        (bool ok, ) = author.call{value: msg.value}("");
        if (!ok) revert TransferFailed();
    }
}
```

Tips flow wallet-to-wallet. Self-dealing through a second wallet is exactly what TigerGraph's funder/WCC analysis catches (Stage 05). The tip still happens, but it earns no LOOP weight.

### 3.4 `contracts/LoopToken.sol`

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract LoopToken is ERC20 {
    constructor(address treasury, uint256 supply) ERC20("CreatorLoop", "LOOP") {
        _mint(treasury, supply);
    }
}
```

In the pitch, describe LOOP as **reputation and reward points on testnet**, not an investment. Fiat value flows through Dodo and the ledger (Stage 07).

### 3.5 `contracts/RewardDistributor.sol`

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";

contract RewardDistributor is AccessControl, ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant PUBLISHER_ROLE = keccak256("PUBLISHER_ROLE");
    bytes32 public constant ENFORCER_ROLE = keccak256("ENFORCER_ROLE"); // freezes accounts

    struct Epoch {
        bytes32 root;
        uint128 total;
        uint128 claimed;
        uint64 publishedAt;
        string allocationsCid;
    }

    IERC20 public immutable token;
    uint256 public immutable maxEpochTotal;
    uint256 public epochCount;
    uint256 public outstanding;

    mapping(uint256 => Epoch) public epochs;
    mapping(uint256 => mapping(address => bool)) public claimed;
    mapping(address => bool) public frozen;

    event EpochPublished(uint256 indexed epochId, bytes32 root, uint256 total, string allocationsCid);
    event Claimed(uint256 indexed epochId, address indexed account, uint256 amount);
    event FrozenSet(address indexed account, bool frozen, bytes32 caseRef);

    error BadRoot();
    error BadTotal();
    error Underfunded();
    error UnknownEpoch();
    error AlreadyClaimed();
    error InvalidProof();
    error EpochOverClaimed();
    error AccountFrozen();

    constructor(IERC20 token_, address admin, address publisher, address enforcer, uint256 maxEpochTotal_) {
        token = token_;
        maxEpochTotal = maxEpochTotal_;
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PUBLISHER_ROLE, publisher);
        _grantRole(ENFORCER_ROLE, enforcer);
    }

    function publishEpoch(bytes32 root, uint256 total, string calldata allocationsCid)
        external onlyRole(PUBLISHER_ROLE) returns (uint256 epochId)
    {
        if (root == bytes32(0)) revert BadRoot();
        if (total == 0 || total > maxEpochTotal) revert BadTotal();
        if (token.balanceOf(address(this)) < outstanding + total) revert Underfunded();
        outstanding += total;
        epochId = ++epochCount;
        epochs[epochId] = Epoch(root, uint128(total), 0, uint64(block.timestamp), allocationsCid);
        emit EpochPublished(epochId, root, total, allocationsCid);
    }

    /// @param caseRef keccak256 of the fraud case id (Stage 05), for auditability without leaking details.
    function setFrozen(address account, bool value, bytes32 caseRef) external onlyRole(ENFORCER_ROLE) {
        frozen[account] = value;
        emit FrozenSet(account, value, caseRef);
    }

    function claim(uint256 epochId, uint256 amount, bytes32[] calldata proof) external nonReentrant {
        if (frozen[msg.sender]) revert AccountFrozen();
        Epoch storage e = epochs[epochId];
        if (e.root == bytes32(0)) revert UnknownEpoch();
        if (claimed[epochId][msg.sender]) revert AlreadyClaimed();
        bytes32 leaf = keccak256(bytes.concat(keccak256(abi.encode(msg.sender, amount))));
        if (!MerkleProof.verifyCalldata(proof, e.root, leaf)) revert InvalidProof();
        if (uint256(e.claimed) + amount > e.total) revert EpochOverClaimed();

        claimed[epochId][msg.sender] = true;
        e.claimed += uint128(amount);
        outstanding -= amount;
        token.safeTransfer(msg.sender, amount);
        emit Claimed(epochId, msg.sender, amount);
    }
}
```

Roles:

| Role | Holder | Blast radius if the key leaks |
|---|---|---|
| `DEFAULT_ADMIN_ROLE` | Deployer, ideally moved to a 2-of-3 Safe afterwards | Full: keep it cold |
| `PUBLISHER_ROLE` | API publisher hot key | At most one bad epoch ≤ `maxEpochTotal`, then revoked |
| `ENFORCER_ROLE` | API enforcer hot key (can equal the publisher for the hackathon) | Can freeze or unfreeze claims; can't move funds |

## 4. Tests (node:test + viem)

```ts
import { describe, it, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { network } from "hardhat";
import { StandardMerkleTree } from "@openzeppelin/merkle-tree";

const { viem } = await network.connect();

describe("RewardDistributor", () => {
  let token: any, dist: any, admin: any, alice: any, bob: any;
  beforeEach(async () => {
    [admin, alice, bob] = await viem.getWalletClients();
    token = await viem.deployContract("LoopToken", [admin.account.address, 10n ** 24n]);
    dist = await viem.deployContract("RewardDistributor",
      [token.address, admin.account.address, admin.account.address, admin.account.address, 10n ** 23n]);
    await token.write.transfer([dist.address, 10n ** 23n]);
  });

  it("pays a valid claim exactly once", async () => {
    const values: [string, bigint][] = [[alice.account.address, 100n], [bob.account.address, 50n]];
    const tree = StandardMerkleTree.of(values, ["address", "uint256"]);
    await dist.write.publishEpoch([tree.root as `0x${string}`, 150n, "bafy-alloc"]);
    await dist.write.claim([1n, 100n, tree.getProof(0)], { account: alice.account });
    assert.equal(await token.read.balanceOf([alice.account.address]), 100n);
    await assert.rejects(dist.write.claim([1n, 100n, tree.getProof(0)], { account: alice.account }));
  });
});
```

### Required cases

**CreatorPosts:**
- [ ] Create a post: emits `PostCreated`, `postCount` increments, `authorOf` is correct.
- [ ] `BadCid` on an empty CID or one over 100 characters.
- [ ] `DailyLimitReached` on the 21st post of a day; it resets the next day.
- [ ] `setHidden`: owner only; `UnknownPost` for a missing post.

**Tipping:**
- [ ] The exact amount is forwarded.
- [ ] `TipTooSmall`, `UnknownPost`, `SelfTip`.
- [ ] A rejecting receiver gives `TransferFailed`.
- [ ] A re-entrant receiver is blocked.

**RewardDistributor:**
- [ ] Valid claim; `AlreadyClaimed`; wrong amount gives `InvalidProof`.
- [ ] `UnknownEpoch`; `Underfunded`; `BadTotal`; `BadRoot`.
- [ ] `frozen` gives `AccountFrozen`; unfreezing allows the claim.
- [ ] `setFrozen` without the role reverts; publish without the role reverts.
- [ ] `outstanding` accounting stays consistent across publishes and claims.
- [ ] A malformed tree whose leaves sum to more than `total`: the final claim reverts `EpochOverClaimed`.
- [ ] **Python parity:** load `test/fixtures/python-tree.json` (generated by Stage 11's `merkle.py`) and claim with its proofs.

## 5. Gas targets

| Function | Target |
|---|---|
| `createPost` | < 90k |
| `tip` | < 45k |
| `claim` | < 95k |
| `setFrozen` | < 50k |

## 6. Acceptance checklist

- [ ] All tests green; coverage at least 90% lines.
- [ ] NatSpec on every external function.
- [ ] No compiler warnings.

## 7. Pitfalls

- **Leaf format** must exactly match OpenZeppelin `StandardMerkleTree` (`keccak256(bytes.concat(keccak256(abi.encode(addr, amount))))`). The Python implementation is tested against it.
- **Freeze timing:** freezing after publication is the backstop. The primary defence is excluding the account from the tree.
- **Don't add upgradeability.** Redeploy if needed; the export scripts make that cheap.
