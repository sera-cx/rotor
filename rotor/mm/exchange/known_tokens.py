"""Addresses and decimals of the tokens Sera lists on Ethereum mainnet.

Pinned so a wrong or hostile SERA_BASE_URL cannot swap one listed token for
another in GET /markets (pricing a bid for XSGD while it buys IDRT, say).
Taken from mainnet GET /tokens on 2026-10-21 and matched, all 40, against the
Sera explorer's token list, which is derived from chain data. A token Sera
lists later is not pinned and is accepted as /markets reports it; add it
here the same way. Sepolia is not pinned: its tokens carry no value.
"""

from __future__ import annotations

# chain id -> token symbol -> (lowercase address, decimals)
KNOWN_TOKENS: dict[int, dict[str, tuple[str, int]]] = {
    1: {
        "AEDZ": ("0xfc347c996bd66c1d92e2045c80b413ef3fc84a90", 6),
        "AUDD": ("0x4cce605ed955295432958d8951d0b176c10720d5", 6),
        "AUDF": ("0xd2a530170d71a9cfe1651fb468e2b98f7ed7456b", 6),
        "AUDM": ("0x081599e4936d12c46bd48913b2329115cd26cbdd", 18),
        "AUDX": ("0xd687759f35bb747a29246a4b9495c8f52c49e00c", 18),
        "AXCNH": ("0x2925ac3be7d585874b88ea51ed50add376ad8239", 6),
        "BRLV": ("0xd7ca0e2c36d647446b782d1b72308e598373e2f5", 18),
        "BRZ": ("0x01d33fd36ec67c6ada32cf36b31e88ee190b1839", 18),
        "CADC": ("0xcadc0acd4b445166f12d2c07eac6e2544fbe2eef", 18),
        "CHFAU": ("0xbd4dfc058eb95b8de5ceaf39966a1a70f5556f78", 6),
        "CNGN": ("0x17cdb2a01e7a34cbb3dd4b83260b05d0274c8dab", 6),
        "EUR0": ("0x3c89cd1884e7bef73ca3ef08d2ef6ec338fd8e49", 18),
        "EURAU": ("0x4933a85b5b5466fbaf179f72d3de273c287ec2c2", 6),
        "EURC": ("0x1abaea1f7c830bd89acc67ec4af516284b1bc33c", 6),
        "EURE": ("0x39b8b6385416f4ca36a20319f70d28621895279d", 18),
        "EURI": ("0x9d1a7a3191102e9f900faa10540837ba84dcbae7", 18),
        "EUROP": ("0x888883b5f5d21fb10dfeb70e8f9722b9fb0e5e51", 6),
        "EURQ": ("0x8df723295214ea6f21026eeeb4382d475f146f9f", 6),
        "EURR": ("0x50753cfaf86c094925bf976f218d043f8791e408", 6),
        "EURS": ("0xdb25f211ab05b1c97d595516f45794528a807ad8", 2),
        "IDRT": ("0x998ffe1e43facffb941dc337dd0468d52ba5b48a", 2),
        "ITRY": ("0xb492b4afd9658093694cf9452d5c272e8230f3b0", 18),
        "JPYC": ("0x431d5dff03120afa4bdf332c61a6e1766ef37bdb", 18),
        "MXNB": ("0xf197ffc28c23e0309b5559e7a166f2c6164c80aa", 6),
        "MXNT": ("0xed03ed872159e199065401b6d0d487d78d9464aa", 6),
        "MYRT": ("0x3fc98a885e99420d0ce43bcb81bf21a4e3f45e5f", 6),
        "NZDD": ("0x2dd087589ce9c5b2d1b42e20d2519b3c8cf022b7", 6),
        "QCAD": ("0x3fa142dd3f384414e05e71ad0939274edc82ec0a", 6),
        "TGBP": ("0x27f6c8289550fce67f6b50bed1f519966afe5287", 18),
        "TRYB": ("0x2c537e5624e4af88a7ae4060c022609376c8d0eb", 6),
        "USDC": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
        "USDT": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
        "VCHF": ("0x79d4f0232a66c4c91b89c76362016a1707cfbf4f", 18),
        "VEUR": ("0x6ba75d640bebfe5da1197bb5a2aff3327789b5d3", 18),
        "WARS": ("0x0dc4f92879b7670e5f4e4e6e3c801d229129d90d", 18),
        "WCLP": ("0x61d450a098b6a7f69fc4b98ce68198fe59768651", 18),
        "WCOP": ("0x8a1d45e102e886510e891d2ec656a708991e2d76", 18),
        "WPEN": ("0x4f34c8b3b5fb6d98da888f0fea543d4d9c9f2ebe", 18),
        "XSGD": ("0x70e8de73ce538da2beed35d14187f6959a8eca96", 6),
        "ZARP": ("0xb755506531786c8ac63b756bab1ac387bacb0c04", 18),
    },
}
