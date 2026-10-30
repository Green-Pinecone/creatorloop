// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ICreatorPosts} from "./interfaces/ICreatorPosts.sol";

/// @title Tipping
/// @notice Forwards native POL tips straight to the post author. Holds no funds.
///         Self-dealing via alt wallets is not blocked here; the off-chain trust engine
///         gives such tips zero reward weight.
contract Tipping is ReentrancyGuard {
    ICreatorPosts public immutable posts;
    uint256 public constant MIN_TIP = 0.001 ether;

    event Tipped(uint256 indexed postId, address indexed from, address indexed to, uint256 amount);

    error TipTooSmall();
    error UnknownPost();
    error SelfTip();
    error TransferFailed();

    constructor(ICreatorPosts posts_) {
        posts = posts_;
    }

    /// @notice Tip the author of `postId` with the attached POL.
    /// @param postId Post being tipped.
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
