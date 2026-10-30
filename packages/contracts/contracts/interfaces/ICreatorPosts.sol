// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

interface ICreatorPosts {
    function authorOf(uint256 postId) external view returns (address);
}
