// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {CreatorPosts} from "../CreatorPosts.sol";
import {Tipping} from "../Tipping.sol";

/// @dev Test-only: a contract author that rejects incoming POL.
contract RejectingAuthor {
    function post(CreatorPosts posts, string calldata cid) external returns (uint256) {
        return posts.createPost(cid);
    }

    receive() external payable {
        revert("no thanks");
    }
}

/// @dev Test-only: a contract author that tries to re-enter Tipping when paid.
contract ReentrantAuthor {
    Tipping public tipping;
    uint256 public postId;

    constructor(Tipping tipping_) payable {
        tipping = tipping_;
    }

    function post(CreatorPosts posts, string calldata cid) external {
        postId = posts.createPost(cid);
    }

    receive() external payable {
        // Re-enter with our own funds; nonReentrant must block this.
        tipping.tip{value: 0.001 ether}(postId);
    }
}
