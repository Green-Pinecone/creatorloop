import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { beforeEach, describe, it } from "node:test";
import { StandardMerkleTree } from "@openzeppelin/merkle-tree";
import { network } from "hardhat";
import { keccak256, toHex, zeroHash } from "viem";

const { viem } = await network.create();

const E18 = 10n ** 18n;
const MAX_EPOCH = 100_000n * E18;
const FUNDING = 1_000_000n * E18;

describe("RewardDistributor", () => {
  let token: any;
  let dist: any;
  let admin: any;
  let alice: any;
  let bob: any;
  let carol: any;
  let mallory: any;

  beforeEach(async () => {
    [admin, alice, bob, carol, mallory] = await viem.getWalletClients();
    token = await viem.deployContract("LoopToken", [admin.account.address, 100_000_000n * E18]);
    dist = await viem.deployContract("RewardDistributor", [
      token.address,
      admin.account.address,
      admin.account.address, // publisher
      admin.account.address, // enforcer
      MAX_EPOCH,
    ]);
    await token.write.transfer([dist.address, FUNDING]);
  });

  function tree(values: [string, bigint][]) {
    return StandardMerkleTree.of(values, ["address", "uint256"]);
  }

  async function publish(values: [string, bigint][], total?: bigint) {
    const t = tree(values);
    const sum = total ?? values.reduce((s, [, v]) => s + v, 0n);
    await dist.write.publishEpoch([t.root, sum, "bafy-alloc"]);
    return t;
  }

  it("pays valid claims exactly once and tracks outstanding", async () => {
    const values: [string, bigint][] = [
      [alice.account.address, 100n * E18],
      [bob.account.address, 50n * E18],
      [carol.account.address, 25n * E18],
    ];
    const t = await publish(values);
    assert.equal(await dist.read.outstanding(), 175n * E18);

    await viem.assertions.emitWithArgs(
      dist.write.claim([1n, values[0][1], t.getProof(0)], { account: alice.account }),
      dist,
      "Claimed",
      [1n, alice.account.address, values[0][1]],
    );
    assert.equal(await token.read.balanceOf([alice.account.address]), 100n * E18);
    assert.equal(await dist.read.outstanding(), 75n * E18);

    await viem.assertions.revertWithCustomError(
      dist.write.claim([1n, values[0][1], t.getProof(0)], { account: alice.account }),
      dist,
      "AlreadyClaimed",
    );
  });

  it("rejects wrong amounts, wrong claimers and unknown epochs", async () => {
    const values: [string, bigint][] = [
      [alice.account.address, 100n],
      [bob.account.address, 50n],
    ];
    const t = await publish(values);
    await viem.assertions.revertWithCustomError(
      dist.write.claim([1n, 101n, t.getProof(0)], { account: alice.account }),
      dist,
      "InvalidProof",
    );
    await viem.assertions.revertWithCustomError(
      dist.write.claim([1n, 100n, t.getProof(0)], { account: mallory.account }),
      dist,
      "InvalidProof",
    );
    await viem.assertions.revertWithCustomError(
      dist.write.claim([2n, 100n, t.getProof(0)], { account: alice.account }),
      dist,
      "UnknownEpoch",
    );
  });

  it("validates publish inputs and funding", async () => {
    await viem.assertions.revertWithCustomError(
      dist.write.publishEpoch([zeroHash, 1n, "x"]),
      dist,
      "BadRoot",
    );
    const t = tree([[alice.account.address, 1n]]);
    await viem.assertions.revertWithCustomError(dist.write.publishEpoch([t.root, 0n, "x"]), dist, "BadTotal");
    await viem.assertions.revertWithCustomError(
      dist.write.publishEpoch([t.root, MAX_EPOCH + 1n, "x"]),
      dist,
      "BadTotal",
    );

    // Fund only enough for 10 full epochs, then the 11th must fail.
    for (let i = 0; i < 10; i++) await dist.write.publishEpoch([t.root, MAX_EPOCH, "x"]);
    await viem.assertions.revertWithCustomError(dist.write.publishEpoch([t.root, 1n, "x"]), dist, "Underfunded");
  });

  it("restricts publishing and freezing to their roles", async () => {
    const t = tree([[alice.account.address, 1n]]);
    await viem.assertions.revertWithCustomError(
      dist.write.publishEpoch([t.root, 1n, "x"], { account: mallory.account }),
      dist,
      "AccessControlUnauthorizedAccount",
    );
    await viem.assertions.revertWithCustomError(
      dist.write.setFrozen([alice.account.address, true, zeroHash], { account: mallory.account }),
      dist,
      "AccessControlUnauthorizedAccount",
    );
  });

  it("blocks frozen accounts and allows them again after unfreezing", async () => {
    const values: [string, bigint][] = [
      [alice.account.address, 10n],
      [bob.account.address, 20n],
    ];
    const t = await publish(values);
    const caseRef = keccak256(toHex("case-123"));
    await viem.assertions.emitWithArgs(
      dist.write.setFrozen([alice.account.address, true, caseRef]),
      dist,
      "FrozenSet",
      [alice.account.address, true, caseRef],
    );
    await viem.assertions.revertWithCustomError(
      dist.write.claim([1n, 10n, t.getProof(0)], { account: alice.account }),
      dist,
      "AccountFrozen",
    );
    await dist.write.setFrozen([alice.account.address, false, caseRef]);
    await dist.write.claim([1n, 10n, t.getProof(0)], { account: alice.account });
  });

  it("caps an epoch at its total even if the tree over-allocates", async () => {
    // Malformed tree: leaves sum to 150 but total published as 100.
    const values: [string, bigint][] = [
      [alice.account.address, 100n],
      [bob.account.address, 50n],
    ];
    const t = await publish(values, 100n);
    await dist.write.claim([1n, 100n, t.getProof(0)], { account: alice.account });
    await viem.assertions.revertWithCustomError(
      dist.write.claim([1n, 50n, t.getProof(1)], { account: bob.account }),
      dist,
      "EpochOverClaimed",
    );
  });

  it("keeps claims independent across epochs", async () => {
    const v1: [string, bigint][] = [[alice.account.address, 5n], [bob.account.address, 6n]];
    const v2: [string, bigint][] = [[alice.account.address, 7n], [bob.account.address, 8n]];
    const t1 = await publish(v1);
    const t2 = await publish(v2);
    await dist.write.claim([1n, 5n, t1.getProof(0)], { account: alice.account });
    await dist.write.claim([2n, 7n, t2.getProof(0)], { account: alice.account });
    // epoch-1 proof is not valid for epoch 2
    await viem.assertions.revertWithCustomError(
      dist.write.claim([2n, 6n, t1.getProof(1)], { account: bob.account }),
      dist,
      "InvalidProof",
    );
    assert.equal(await token.read.balanceOf([alice.account.address]), 12n);
  });

  // Produced by services/api (Stage 11 merkle.py). Skipped until that fixture exists.
  const fixturePath = path.resolve(import.meta.dirname, "fixtures/python-tree.json");
  it("accepts proofs from the Python Merkle implementation", { skip: !existsSync(fixturePath) }, async () => {
    const fx = JSON.parse(readFileSync(fixturePath, "utf8")) as {
      root: `0x${string}`;
      values: { privateKeyIndex: number; amount: string; proof: `0x${string}`[] }[];
    };
    const wallets = await viem.getWalletClients();
    const total = fx.values.reduce((s, v) => s + BigInt(v.amount), 0n);
    await dist.write.publishEpoch([fx.root, total, "bafy-python"]);
    for (const v of fx.values) {
      await dist.write.claim([1n, BigInt(v.amount), v.proof], { account: wallets[v.privateKeyIndex].account });
    }
    assert.equal(await dist.read.outstanding(), 0n);
  });
});
