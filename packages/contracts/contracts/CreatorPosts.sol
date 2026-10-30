// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ICreatorPosts} from "./interfaces/ICreatorPosts.sol";

/// @title CreatorPosts
/// @notice Anchors creator posts on-chain. The IPFS metadata CID lives only in the event;
///         off-chain indexers own the history. Engagement (reactions, follows) is off-chain.
contract CreatorPosts is ICreatorPosts, Ownable2Step {
    struct Post {
        address author;
        uint64 createdAt;
        bool hidden;
    }

    uint256 public constant MAX_CID_LENGTH = 100;

    uint256 public postCount;
    uint256 public maxPostsPerDay = 20;

    mapping(uint256 postId => Post) public posts;
    mapping(address author => mapping(uint256 day => uint256 count)) public postsOnDay;

    event PostCreated(uint256 indexed postId, address indexed author, string metadataCid);
    event PostHidden(uint256 indexed postId, bool hidden);
    event MaxPostsPerDayUpdated(uint256 value);

    error BadCid();
    error UnknownPost();
    error DailyLimitReached();

    constructor(address owner_) Ownable(owner_) {}

    /// @notice Publish a post whose metadata JSON is pinned on IPFS.
    /// @param metadataCid IPFS CID (v1) of the post metadata JSON.
    /// @return postId Sequential id starting at 1.
    function createPost(string calldata metadataCid) external returns (uint256 postId) {
        uint256 len = bytes(metadataCid).length;
        if (len == 0 || len > MAX_CID_LENGTH) revert BadCid();

        uint256 day = block.timestamp / 1 days; // UTC day bucket
        if (++postsOnDay[msg.sender][day] > maxPostsPerDay) revert DailyLimitReached();

        postId = ++postCount;
        posts[postId] = Post({author: msg.sender, createdAt: uint64(block.timestamp), hidden: false});
        emit PostCreated(postId, msg.sender, metadataCid);
    }

    /// @notice Moderation: hide or unhide a post (indexers stop surfacing hidden posts).
    /// @param postId Post to update.
    /// @param hidden New hidden flag.
    function setHidden(uint256 postId, bool hidden) external onlyOwner {
        if (posts[postId].author == address(0)) revert UnknownPost();
        posts[postId].hidden = hidden;
        emit PostHidden(postId, hidden);
    }

    /// @notice Update the per-author daily post cap.
    /// @param value New cap.
    function setMaxPostsPerDay(uint256 value) external onlyOwner {
        maxPostsPerDay = value;
        emit MaxPostsPerDayUpdated(value);
    }

    /// @notice Author of a post, or the zero address if it does not exist.
    /// @param postId Post id.
    function authorOf(uint256 postId) external view returns (address) {
        return posts[postId].author;
    }
}
