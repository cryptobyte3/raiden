pragma solidity ^0.4.11;

contract Utils {
    uint constant public contract_version = 1;
    /// @notice Check if a contract exists
    /// @param channel The address to check whether a contract is deployed or not
    /// @return True if a contract exists, false otherwise
    function contractExists(address channel) constant returns (bool) {
        uint size;

        assembly {
            size := extcodesize(channel)
        }

        return size > 0;
    }
}
