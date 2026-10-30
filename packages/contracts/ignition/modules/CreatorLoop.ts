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
