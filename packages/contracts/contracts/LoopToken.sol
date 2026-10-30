// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @title LoopToken
/// @notice CreatorLoop reward points (testnet). Fixed supply minted to the treasury.
contract LoopToken is ERC20 {
    /// @param treasury Receives the full supply.
    /// @param supply Total supply in wei units.
    constructor(address treasury, uint256 supply) ERC20("CreatorLoop", "LOOP") {
        _mint(treasury, supply);
    }
}
