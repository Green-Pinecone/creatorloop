import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";
import { network } from "hardhat";
import { parseEther } from "viem";

const { viem } = await network.create();

describe("Tipping", () => {
  let posts: any;
  let tipping: any;
  let owner: any;
  let author: any;
  let fan: any;
  let publicClient: any;

  beforeEach(async () => {
    [owner, author, fan] = await viem.getWalletClients();
    publicClient = await viem.getPublicClient();
    posts = await viem.deployContract("CreatorPosts", [owner.account.address]);
    tipping = await viem.deployContract("Tipping", [posts.address]);
    await posts.write.createPost(["cid"], { account: author.account });
  });

  it("forwards the exact tip to the author and keeps nothing", async () => {
    const amount = parseEther("0.05");
    await viem.assertions.balancesHaveChanged(
      tipping.write.tip([1n], { account: fan.account, value: amount }),
      [{ address: author.account.address, amount }],
    );
    assert.equal(await publicClient.getBalance({ address: tipping.address }), 0n);
  });

  it("emits Tipped", async () => {
    const amount = parseEther("0.01");
    await viem.assertions.emitWithArgs(
      tipping.write.tip([1n], { account: fan.account, value: amount }),
      tipping,
      "Tipped",
      [1n, fan.account.address, author.account.address, amount],
    );
  });

  it("rejects tiny tips, unknown posts and self tips", async () => {
    await viem.assertions.revertWithCustomError(
      tipping.write.tip([1n], { account: fan.account, value: parseEther("0.0009") }),
      tipping,
      "TipTooSmall",
    );
    await viem.assertions.revertWithCustomError(
      tipping.write.tip([99n], { account: fan.account, value: parseEther("0.01") }),
      tipping,
      "UnknownPost",
    );
    await viem.assertions.revertWithCustomError(
      tipping.write.tip([1n], { account: author.account, value: parseEther("0.01") }),
      tipping,
      "SelfTip",
    );
  });

  it("reverts when the author rejects payment", async () => {
    const rejecting = await viem.deployContract("RejectingAuthor");
    await rejecting.write.post([posts.address, "cid-rejecting"]);
    await viem.assertions.revertWithCustomError(
      tipping.write.tip([2n], { account: fan.account, value: parseEther("0.01") }),
      tipping,
      "TransferFailed",
    );
  });

  it("blocks re-entrancy from a malicious author", async () => {
    const reentrant = await viem.deployContract("ReentrantAuthor", [tipping.address], {
      value: parseEther("1"),
    });
    await reentrant.write.post([posts.address, "cid-reentrant"]);
    // The nested tip hits nonReentrant → receive() reverts → outer call sees TransferFailed.
    await viem.assertions.revertWithCustomError(
      tipping.write.tip([2n], { account: fan.account, value: parseEther("0.01") }),
      tipping,
      "TransferFailed",
    );
    assert.equal(await publicClient.getBalance({ address: tipping.address }), 0n);
  });
});
