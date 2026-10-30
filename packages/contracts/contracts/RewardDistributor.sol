// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";

/// @title RewardDistributor
/// @notice Per-epoch Merkle distribution of LOOP. Allocations are computed off-chain with
///         trust weighting; frozen accounts (fraud cases) are blocked from claiming as a backstop.
contract RewardDistributor is AccessControl, ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant PUBLISHER_ROLE = keccak256("PUBLISHER_ROLE");
    bytes32 public constant ENFORCER_ROLE = keccak256("ENFORCER_ROLE");

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
    /// @notice Published but not yet claimed amount across all epochs.
    uint256 public outstanding;

    mapping(uint256 epochId => Epoch) public epochs;
    mapping(uint256 epochId => mapping(address account => bool)) public claimed;
    mapping(address account => bool) public frozen;

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

    /// @param token_ Reward token.
    /// @param admin Holder of DEFAULT_ADMIN_ROLE.
    /// @param publisher Scorer hot key allowed to publish epochs.
    /// @param enforcer Hot key allowed to freeze/unfreeze accounts.
    /// @param maxEpochTotal_ Upper bound per epoch; limits damage from a leaked publisher key.
    constructor(IERC20 token_, address admin, address publisher, address enforcer, uint256 maxEpochTotal_) {
        token = token_;
        maxEpochTotal = maxEpochTotal_;
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PUBLISHER_ROLE, publisher);
        _grantRole(ENFORCER_ROLE, enforcer);
    }

    /// @notice Publish a new epoch's Merkle root. Must be fully funded.
    /// @param root Root over leaves keccak256(bytes.concat(keccak256(abi.encode(account, amount)))).
    /// @param total Sum of all leaf amounts.
    /// @param allocationsCid IPFS CID of the public allocations JSON.
    /// @return epochId Sequential id starting at 1.
    function publishEpoch(bytes32 root, uint256 total, string calldata allocationsCid)
        external
        onlyRole(PUBLISHER_ROLE)
        returns (uint256 epochId)
    {
        if (root == bytes32(0)) revert BadRoot();
        if (total == 0 || total > maxEpochTotal) revert BadTotal();
        if (token.balanceOf(address(this)) < outstanding + total) revert Underfunded();

        outstanding += total;
        epochId = ++epochCount;
        epochs[epochId] = Epoch({
            root: root,
            total: uint128(total),
            claimed: 0,
            publishedAt: uint64(block.timestamp),
            allocationsCid: allocationsCid
        });
        emit EpochPublished(epochId, root, total, allocationsCid);
    }

    /// @notice Freeze or unfreeze an account's ability to claim.
    /// @param account Account to update.
    /// @param value New frozen flag.
    /// @param caseRef keccak256 of the off-chain fraud case id, for auditability.
    function setFrozen(address account, bool value, bytes32 caseRef) external onlyRole(ENFORCER_ROLE) {
        frozen[account] = value;
        emit FrozenSet(account, value, caseRef);
    }

    /// @notice Claim an epoch allocation.
    /// @param epochId Epoch to claim from.
    /// @param amount Allocated amount (must match the leaf).
    /// @param proof Merkle proof for (msg.sender, amount).
    function claim(uint256 epochId, uint256 amount, bytes32[] calldata proof) external nonReentrant {
        if (frozen[msg.sender]) revert AccountFrozen();
        Epoch storage e = epochs[epochId];
        if (e.root == bytes32(0)) revert UnknownEpoch();
        if (claimed[epochId][msg.sender]) revert AlreadyClaimed();

        // OpenZeppelin StandardMerkleTree leaf format.
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
