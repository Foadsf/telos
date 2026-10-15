"""Zero-dependency, lexicographically sortable, monotonic ULID generator."""

from __future__ import annotations

import os
import threading
import time

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LOCK = threading.Lock()
_LAST_MS: int = 0
_LAST_ENTROPY: int = 0

def _encode_base32(value: int, length: int) -> str:
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)

def new_ulid() -> str:
    """Generate a 26-character monotonic ULID string."""
    global _LAST_MS, _LAST_ENTROPY
    now_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    with _LOCK:
        if now_ms == _LAST_MS:
            _LAST_ENTROPY = (_LAST_ENTROPY + 1) & ((1 << 80) - 1)
            entropy = _LAST_ENTROPY
        else:
            _LAST_MS = now_ms
            entropy = int.from_bytes(os.urandom(10), byteorder="big")
            _LAST_ENTROPY = entropy
    return _encode_base32(now_ms, 10) + _encode_base32(entropy, 16)
