"""Pinned production identities and monitor policy constants."""

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

TELLOR_MASTER = "0x88df592f8eb5d7bd38bfef7deb0fbc02cf3778a0"
TELLOR_FLEX = "0x8cfc184c877154a8f9ffe0fe75649dbe5e2dbebf"
GOVERNANCE = "0xb30b1b98d8276b80bc4f5af9f9170ef3220ec27d"
TOKEN_BRIDGE_V1 = "0x5589e306b1920f009979a50b88cae32aecd471e4"
TOKEN_BRIDGE_V2 = "0x6ec401744008f4b018ed9a36f76e6629799ee50e"
TELLOR_DATA_BRIDGE = "0xffa3393be1e4b442fff6cd0df0794b0031e9cf65"
TELLOR_DATA_BANK = "0x5526e7e7cf982f5d3045daf18b7e813e4b7a8fe5"

BRIDGES = frozenset({TOKEN_BRIDGE_V1, TOKEN_BRIDGE_V2})

MONITORS = {
    "tellormaster-control": ("M1", "P0"),
    "bridge-control": ("M2", "P0"),
    "databridge-integrity": ("M3", "P0"),
    "bridge-ledger-integrity": ("M4", "P0"),
    "tellorflex-value-integrity": ("M5", "P1"),
    "databank-value-integrity": ("M6", "P0"),
    "governance-dispute": ("M7", "P1"),
    "issuance-integrity": ("M8", "P0"),
    "tellorflex-eth-usd-freshness": ("M9", "P1"),
    "tellorflex-ampl-usd-deadline": ("M10", "P1"),
    "tellorflex-uspce-deadline": ("M11", "P1"),
}

EVM_SENSOR_SLUGS = frozenset(tuple(MONITORS)[:8])

ETH_USD_QUERY_ID = (
    "0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992"
)
AMPL_USD_QUERY_ID = (
    "0x0d12ad49193163bbbeff4e6db8294ced23ff8605359fd666799d4e25a3aa0e3a"
)
USPCE_QUERY_ID = (
    "0x612ec1d9cee860bb87deb6370ed0ae43345c9302c085c1dfc4c207cbec2970d7"
)

FIXED_QUERY_TYPES = {
    "0x3ab34a189e35885414ac4e83c5a7faa9d8f03a4d530728ef516d203d91d6309c": (
        "AutopayAddresses",
        "address[]",
    ),
    "0xcf0c5863be1cf3b948a9ff43290f931399765d051a60c3b23a4e098148b1f707": (
        "TellorOracleAddress",
        "address",
    ),
    AMPL_USD_QUERY_ID: ("AmpleforthCustomSpotPrice", "uint256"),
    USPCE_QUERY_ID: ("AmpleforthUSPCE", "uint256"),
}

M6_SPOT_QUERY_IDS = frozenset(
    {
        "0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac",
        ETH_USD_QUERY_ID,
        "0x5c13cd9c97dbb98f2429c101a2a8150e6c7a0ddaff6124ee176a3a411067ded0",
        "0x40aa71e5205fdc7bdb7d65f7ae41daca3820c5d3a8f62357a99eda3aa27244a3",
        "0x68584962e7ca6a57d672cdbfaa37c55431a84c5bb8c40d5d204a23f304f83b2e",
        "0x50f84b680a867b18b936bb22eac2dffc07a235fc125a106179f37f31bb3d86e3",
        "0x19585d912afb72378e3986a7a53f1eae1fbae792cd17e1d0df063681326823ae",
        "0xafc6a3f6c18df31f1078cf038745b48e55623330715d90efe3dc7935efd44938",
        "0xefa84ae5ea9eb0545e159f78f0a44911ac5a81ecb6ff0c4e32107bcfc66c4baa",
        "0xb211d6f1abbd5bb431618547402a92250b765151acbe749e7f9c26dc19e5dd9a",
        "0x8810ffb0cfcb6131da29ed4b229f252d6bac6fc98fc4a61ffbde5b48131e0228",
        "0x537422e5383888586f8f9bca62c5bfd8eb0f8c1bcd335b1a691e6b550c92dcce",
        "0x7f3fc5bbf0bcc372beece1d2711095b6c884c69e21dad1180f2160adfcd8b044",
        "0x2c81613b335c890096fd1c9a89766a2d71da2c9636505a9cb3b3dc7877cdad4b",
        "0x907154958baee4fb0ce2bbe50728141ac76eb2dc1731b3d40f0890746dd07e62",
        "0x1962cde2f19178fe2bb2229e78a6d386e6406979edc7b9a1966d89d83b3ebf2e",
        "0xfd47fa335a8c4886222ebae89a8de8d4a0187eb06c4429d3c0a7932332d2430d",
        "0xbb5e0a51ab0e06354439f377e326ca71ec8149249d163f75f543fcdc25818e76",
    }
)

EVM_CALL_CHAIN_ENVS = {
    1: "RPC_ETHEREUM_MAINNET",
    10: "RPC_OPTIMISM",
    100: "RPC_GNOSIS",
    137: "RPC_POLYGON",
    10200: "RPC_CHIADO",
    11155111: "RPC_SEPOLIA",
}

VALIDATOR_DOMAIN = bytes.fromhex(
    "636865636b706f696e7400000000000000000000000000000000000000000000"
)
EIP_IMPLEMENTATION_SLOT = (
    "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
)

FIRST_ACTIONS = {
    "tellormaster-control": (
        "Stop bridge and issuance automation, verify the controlling action, and "
        "inspect the target bytecode before another transaction."
    ),
    "bridge-control": (
        "Stop the affected bridge automation and compare the action with the "
        "reviewed release manifest."
    ),
    "databridge-integrity": (
        "Stop bridge relaying, preserve calldata and signatures, and compare the "
        "Tellor Layer validator set."
    ),
    "bridge-ledger-integrity": (
        "Stop the affected relayer or claim path and preserve both chain records."
    ),
    "tellorflex-value-integrity": (
        "Preserve the query, value, and reference evidence and prepare a dispute review."
    ),
    "databank-value-integrity": (
        "Stop consumers from accepting the affected DataBank value and preserve the "
        "attestation and reference evidence."
    ),
    "governance-dispute": (
        "Identify consumers of the query and timestamp, preserve the disputed value, "
        "and review the dispute evidence and voting deadline."
    ),
    "issuance-integrity": (
        "Stop bridge and issuance automation, preserve the transaction or block "
        "evidence, and reconcile the implementation version."
    ),
    "tellorflex-eth-usd-freshness": (
        "Inspect the ETH/USD reporting job and open disputes, then restore reporting."
    ),
    "tellorflex-ampl-usd-deadline": (
        "Inspect the AMPL reporting schedule and disputes, then restore the next window."
    ),
    "tellorflex-uspce-deadline": (
        "Inspect the USPCE reporting schedule and disputes, then restore the next deadline."
    ),
}
