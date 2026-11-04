"""Bencode encoding and decoding as used by BitTorrent (BEP 3)."""

from __future__ import annotations

import re
from typing import Any

MAX_DEPTH = 512

_INT_RE = re.compile(rb"(?:0|-?[1-9][0-9]*)\Z")


class BencodeError(ValueError):
    """Raised when a payload is not valid bencode."""


def encode(value: Any) -> bytes:
    """Encode ``value`` to bytes.

    Supported types: ``int``, ``bytes``, ``str`` (UTF-8), ``list``, ``tuple``
    and ``dict`` (keys may be ``bytes`` or ``str``, sorted as raw byte strings
    as BEP 3 requires).
    """
    out = bytearray()
    _encode(value, out)
    return bytes(out)


def _encode(value: Any, out: bytearray) -> None:
    if isinstance(value, bool):
        raise TypeError("bencode has no boolean type")
    if isinstance(value, int):
        out += b"i%de" % value
    elif isinstance(value, bytes):
        out += str(len(value)).encode("ascii")
        out += b":"
        out += value
    elif isinstance(value, bytearray):
        _encode(bytes(value), out)
    elif isinstance(value, str):
        _encode(value.encode("utf-8"), out)
    elif isinstance(value, (list, tuple)):
        out += b"l"
        for item in value:
            _encode(item, out)
        out += b"e"
    elif isinstance(value, dict):
        items = []
        for key, item in value.items():
            if isinstance(key, str):
                key = key.encode("utf-8")
            if not isinstance(key, bytes):
                raise TypeError(f"bencode dict keys must be bytes or str, got {type(key).__name__}")
            items.append((key, item))
        # Sort by the raw key bytes, not the "len:key" form: "6:length" would
        # otherwise sort before "4:name" and every info hash would be wrong.
        items.sort(key=lambda pair: pair[0])
        out += b"d"
        for key, item in items:
            _encode(key, out)
            _encode(item, out)
        out += b"e"
    else:
        raise TypeError(f"cannot bencode {type(value).__name__}")


def decode(data: bytes, *, strict: bool = True) -> Any:
    """Decode a complete bencoded payload.

    With ``strict=True`` trailing bytes raise :class:`BencodeError`; this is
    the safe default for ``.torrent`` files.
    """
    if isinstance(data, memoryview):
        data = bytes(data)
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("decode() expects bytes")
    payload = bytes(data)
    value, index = _decode(payload, 0, 0)
    if strict and index != len(payload):
        raise BencodeError(f"trailing data at offset {index}")
    return value


def decode_prefix(data: bytes, offset: int = 0) -> tuple[Any, int]:
    """Decode one value starting at ``offset`` and return it with the end index.

    Used for protocols that append raw bytes after a bencoded header
    (for example ``ut_metadata`` messages).
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("decode_prefix() expects bytes")
    return _decode(bytes(data), offset, 0)


def raw_value_span(data: bytes, key: bytes) -> tuple[int, int] | None:
    """Return ``(start, end)`` of ``key``'s value in a top-level bencoded dict.

    Info hashes must be computed over the exact bytes in the ``.torrent``
    file, so callers slice the original payload instead of re-encoding.
    """
    payload = bytes(data)
    if not payload.startswith(b"d"):
        return None
    index = 1
    while index < len(payload) and payload[index] != 0x65:
        current, index = _decode(payload, index, 1)
        start = index
        _value, index = _decode(payload, index, 1)
        if current == key:
            return start, index
    return None


def _decode(data: bytes, index: int, depth: int) -> tuple[Any, int]:
    if depth > MAX_DEPTH:
        raise BencodeError("nesting too deep")
    if index >= len(data):
        raise BencodeError("unexpected end of data")
    marker = data[index]

    if marker == 0x69:  # b"i"
        end = data.find(b"e", index + 1)
        if end < 0:
            raise BencodeError("unterminated integer")
        raw = data[index + 1 : end]
        if not _INT_RE.match(raw):
            raise BencodeError(f"invalid integer {raw!r}")
        return int(raw), end + 1

    if marker == 0x6C:  # b"l"
        items = []
        index += 1
        while True:
            if index >= len(data):
                raise BencodeError("unterminated list")
            if data[index] == 0x65:  # b"e"
                return items, index + 1
            item, index = _decode(data, index, depth + 1)
            items.append(item)

    if marker == 0x64:  # b"d"
        result: dict[bytes, Any] = {}
        index += 1
        while True:
            if index >= len(data):
                raise BencodeError("unterminated dictionary")
            if data[index] == 0x65:
                return result, index + 1
            key, index = _decode(data, index, depth + 1)
            if not isinstance(key, bytes):
                raise BencodeError("dictionary keys must be strings")
            if key in result:
                raise BencodeError(f"duplicate dictionary key {key!r}")
            value, index = _decode(data, index, depth + 1)
            result[key] = value

    if 0x30 <= marker <= 0x39:  # digit
        colon = data.find(b":", index)
        if colon < 0:
            raise BencodeError("unterminated string length")
        raw_len = data[index:colon]
        if not raw_len.isdigit():
            raise BencodeError(f"invalid string length {raw_len!r}")
        if len(raw_len) > 1 and raw_len.startswith(b"0"):
            raise BencodeError("string length has leading zeros")
        length = int(raw_len)
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeError("string extends past end of data")
        return data[start:end], end

    raise BencodeError(f"unexpected byte 0x{marker:02x} at offset {index}")
