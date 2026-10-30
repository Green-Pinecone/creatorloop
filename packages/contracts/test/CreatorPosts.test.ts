import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";
import { network } from "hardhat";

const { viem, networkHelpers } = await network.create();

describe("CreatorPosts", () => {
  let posts: any;
  let owner: any;
  let alice: any;
  let bob: any;

  beforeEach(async () => {
    [owner, alice, bob] = await viem.getWalletClients();
    posts = await viem.deployContract("CreatorPosts", [owner.account.address]);
  });

  it("creates a post, emits the CID and records the author", async () => {
    await viem.assertions.emitWithArgs(
      posts.write.createPost(["bafy-meta-1"], { account: alice.account }),
      posts,
      "PostCreated",
      [1n, alice.account.address, "bafy-meta-1"],
    );
    assert.equal(await posts.read.postCount(), 1n);
    assert.equal(
      (await posts.read.authorOf([1n])).toLowerCase(),
      alice.account.address.toLowerCase(),
    );
  });

  it("rejects empty and over-long CIDs", async () => {
    await viem.assertions.revertWithCustomError(
      posts.write.createPost([""], { account: alice.account }),
      posts,
      "BadCid",
    );
    await viem.assertions.revertWithCustomError(
      posts.write.createPost(["x".repeat(101)], { account: alice.account }),
      posts,
      "BadCid",
    );
    // exactly 100 is fine
    await posts.write.createPost(["x".repeat(100)], { account: alice.account });
  });

  it("enforces the daily cap and resets the next day", async () => {
    await posts.write.setMaxPostsPerDay([3n]);
    for (let i = 0; i < 3; i++) await posts.write.createPost([`cid-${i}`], { account: alice.account });
    await viem.assertions.revertWithCustomError(
      posts.write.createPost(["cid-4"], { account: alice.account }),
      posts,
      "DailyLimitReached",
    );
    // a different author is unaffected
    await posts.write.createPost(["bob-1"], { account: bob.account });

    await networkHelpers.time.increase(24 * 60 * 60);
    await posts.write.createPost(["cid-next-day"], { account: alice.account });
  });

  it("lets only the owner hide posts", async () => {
    await posts.write.createPost(["cid"], { account: alice.account });
    await viem.assertions.revertWithCustomError(
      posts.write.setHidden([1n, true], { account: alice.account }),
      posts,
      "OwnableUnauthorizedAccount",
    );
    await viem.assertions.emitWithArgs(posts.write.setHidden([1n, true]), posts, "PostHidden", [1n, true]);
    const [, , hidden] = await posts.read.posts([1n]);
    assert.equal(hidden, true);
  });

  it("rejects hiding unknown posts", async () => {
    await viem.assertions.revertWithCustomError(posts.write.setHidden([42n, true]), posts, "UnknownPost");
  });

  it("only the owner can change the daily cap", async () => {
    await viem.assertions.revertWithCustomError(
      posts.write.setMaxPostsPerDay([1n], { account: bob.account }),
      posts,
      "OwnableUnauthorizedAccount",
    );
    await viem.assertions.emitWithArgs(posts.write.setMaxPostsPerDay([5n]), posts, "MaxPostsPerDayUpdated", [5n]);
  });
});
