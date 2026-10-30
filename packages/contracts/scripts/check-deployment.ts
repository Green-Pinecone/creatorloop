// Post-deploy assertions. Usage:
//   pnpm hardhat run scripts/check-deployment.ts --network amoy
// Reads ignition/deployments/<DEPLOYMENT_ID>/ (default "amoy") and ignition/parameters/<id>.json.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { network } from "hardhat";
import { keccak256, toHex } from "viem";

const id = process.env.DEPLOYMENT_ID ?? "amoy";
const root = path.resolve(import.meta.dirname, "..");
const addrs = JSON.parse(
  readFileSync(path.join(root, "ignition/deployments", id, "deployed_addresses.json"), "utf8"),
);
const params = JSON.parse(readFileSync(path.join(root, "ignition/parameters", `${id}.json`), "utf8")).CreatorLoop;
const a = (n: string) => addrs[`CreatorLoop#${n}`] as `0x${string}`;
const eq = (x: string, y: string) => x.toLowerCase() === y.toLowerCase();

const { viem } = await network.create();
const [deployer] = await viem.getWalletClients();
const posts = await viem.getContractAt("CreatorPosts", a("CreatorPosts"));
const tipping = await viem.getContractAt("Tipping", a("Tipping"));
const token = await viem.getContractAt("LoopToken", a("LoopToken"));
const dist = await viem.getContractAt("RewardDistributor", a("RewardDistributor"));

const checks: [string, () => Promise<void>][] = [
  ["Tipping.posts == CreatorPosts", async () => assert.ok(eq(await tipping.read.posts(), posts.address))],
  ["CreatorPosts.owner == deployer", async () => assert.ok(eq(await posts.read.owner(), deployer.account.address))],
  ["Distributor.token == LoopToken", async () => assert.ok(eq(await dist.read.token(), token.address))],
  ["publisher has PUBLISHER_ROLE", async () =>
    assert.ok(await dist.read.hasRole([keccak256(toHex("PUBLISHER_ROLE")), params.publisher]))],
  ["enforcer has ENFORCER_ROLE", async () =>
    assert.ok(await dist.read.hasRole([keccak256(toHex("ENFORCER_ROLE")), params.enforcer]))],
  ["distributor is funded", async () => assert.ok((await token.read.balanceOf([dist.address])) > 0n)],
];

let failed = 0;
for (const [name, fn] of checks) {
  try {
    await fn();
    console.log(`✔ ${name}`);
  } catch (e) {
    failed++;
    console.log(`✘ ${name}: ${(e as Error).message}`);
  }
}
if (failed) process.exit(1);
