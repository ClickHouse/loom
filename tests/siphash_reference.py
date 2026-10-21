"""The reference SipHash-2-4-128, written out so the analytics export's hashes can
be checked against an implementation that is not ClickHouse's."""

from __future__ import annotations

_M = (1 << 64) - 1


def _rotl(x: int, b: int) -> int:
    return ((x << b) | (x >> (64 - b))) & _M


def siphash_2_4_128(key: bytes, msg: bytes) -> bytes:
    """SipHash-2-4 with a 128-bit output, as the reference implementation."""
    k0, k1 = int.from_bytes(key[:8], "little"), int.from_bytes(key[8:], "little")
    v = [
        k0 ^ 0x736F6D6570736575,
        k1 ^ 0x646F72616E646F6D ^ 0xEE,
        k0 ^ 0x6C7967656E657261,
        k1 ^ 0x7465646279746573,
    ]

    def rounds(n: int) -> None:
        for _ in range(n):
            v[0] = (v[0] + v[1]) & _M
            v[1] = _rotl(v[1], 13) ^ v[0]
            v[0] = _rotl(v[0], 32)
            v[2] = (v[2] + v[3]) & _M
            v[3] = _rotl(v[3], 16) ^ v[2]
            v[0] = (v[0] + v[3]) & _M
            v[3] = _rotl(v[3], 21) ^ v[0]
            v[2] = (v[2] + v[1]) & _M
            v[1] = _rotl(v[1], 17) ^ v[2]
            v[2] = _rotl(v[2], 32)

    tail = len(msg) % 8
    blocks = [int.from_bytes(msg[i : i + 8], "little") for i in range(0, len(msg) - tail, 8)]
    blocks.append((len(msg) & 0xFF) << 56 | int.from_bytes(msg[len(msg) - tail :], "little"))
    for m in blocks:
        v[3] ^= m
        rounds(2)
        v[0] ^= m
    v[2] ^= 0xEE
    rounds(4)
    first = (v[0] ^ v[1] ^ v[2] ^ v[3]).to_bytes(8, "little")
    v[1] ^= 0xDD
    rounds(4)
    return first + (v[0] ^ v[1] ^ v[2] ^ v[3]).to_bytes(8, "little")
