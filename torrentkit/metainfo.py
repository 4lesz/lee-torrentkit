"""Parsing of BitTorrent metainfo (``.torrent``) and magnet URIs."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import parse_qs, quote

from . import bencode

PIECE_HASH_SIZE = 20
INFO_HASH_SIZE = 20
DEFAULT_MAGNET_SAFE = ":/?#[]@!$'()*+,;=%"


class MetainfoError(ValueError):
    """Raised when metainfo or a magnet URI is malformed or unsafe."""


@dataclass(frozen=True)
class FileEntry:
    """One file inside a torrent.

    ``path`` is relative to the torrent root (``name``) for multi-file
    torrents and empty for single-file torrents. ``offset`` is the position of
    the file in the concatenated torrent byte stream.
    """

    path: tuple[str, ...]
    length: int
    offset: int

    def display(self, name: str, single_file: bool) -> str:
        if single_file:
            return name
        return "/".join((name, *self.path))


def _text(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def _clean_parts(parts: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(parts, (list, tuple)) or not parts:
        raise MetainfoError(f"{field}: missing path")
    result: list[str] = []
    for raw in parts:
        if not isinstance(raw, bytes):
            raise MetainfoError(f"{field}: path component is not bytes")
        text = _text(raw)
        if text in ("", ".", "..") or "/" in text or "\\" in text or "\x00" in text:
            raise MetainfoError(f"{field}: unsafe path component {text!r}")
        result.append(text)
    return tuple(result)


class Torrent:
    """A parsed torrent file.

    Parsing is defensive: weird or unsafe paths raise :class:`MetainfoError`
    instead of producing something that could escape the download directory.
    """

    def __init__(
        self, metainfo: dict, *, source: str | None = None, raw_info: bytes | None = None
    ):
        if not isinstance(metainfo, dict):
            raise MetainfoError("metainfo must be a dictionary")
        info = metainfo.get(b"info")
        if not isinstance(info, dict):
            raise MetainfoError("metainfo has no info dictionary")

        self.metainfo = metainfo
        self.info = info
        self.source = source

        raw_name = info.get(b"name")
        if not isinstance(raw_name, bytes) or not raw_name:
            raise MetainfoError("info dictionary has no name")
        self.name = _text(raw_name)
        if self.name in (".", "..") or "/" in self.name or "\\" in self.name or "\x00" in self.name:
            raise MetainfoError(f"unsafe torrent name {self.name!r}")

        piece_length = info.get(b"piece length")
        if not isinstance(piece_length, int) or piece_length <= 0:
            raise MetainfoError("info dictionary has no valid piece length")
        self.piece_length = piece_length

        meta_version = info.get(b"meta version")
        if meta_version is not None and meta_version not in (1, 2):
            raise MetainfoError(f"unsupported meta version {meta_version!r}")
        self.is_v2 = meta_version == 2

        self._piece_hashes = self._parse_piece_hashes(info)
        self.files = self._parse_files(info)
        self.total_length = sum(entry.length for entry in self.files)

        if self._piece_hashes is not None:
            expected = (self.total_length + piece_length - 1) // piece_length if self.total_length else 0
            if len(self._piece_hashes) != expected:
                raise MetainfoError(
                    f"piece count mismatch: {len(self._piece_hashes)} hashes for {expected} pieces"
                )

        encoded_info = raw_info if raw_info is not None else bencode.encode(info)
        self.info_hash = hashlib.sha1(encoded_info).digest()
        self.info_hash_v2 = hashlib.sha256(encoded_info).digest() if self.is_v2 else None

        self.trackers = self._parse_trackers(metainfo)
        self.web_seeds = self._parse_web_seeds(metainfo)
        self.private = bool(info.get(b"private"))
        raw_comment = metainfo.get(b"comment")
        self.comment = _text(raw_comment) if isinstance(raw_comment, bytes) else None

    # -- construction -----------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes, *, source: str | None = None) -> "Torrent":
        try:
            metainfo = bencode.decode(data)
            span = bencode.raw_value_span(data, b"info")
        except bencode.BencodeError as exc:
            raise MetainfoError(str(exc)) from exc
        raw_info = bytes(data[span[0] : span[1]]) if span else None
        return cls(metainfo, source=source, raw_info=raw_info)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "Torrent":
        with open(path, "rb") as handle:
            data = handle.read()
        return cls.from_bytes(data, source=str(path))

    @classmethod
    def from_info(
        cls,
        info: dict,
        *,
        trackers: Iterable[str] = (),
        web_seeds: Iterable[str] = (),
        comment: str | None = None,
    ) -> "Torrent":
        """Build a torrent from a bare info dictionary (for example metadata
        obtained from the ``ut_metadata`` extension)."""
        metainfo: dict = {b"info": info}
        tracker_list = list(trackers)
        if tracker_list:
            metainfo[b"announce"] = tracker_list[0].encode("utf-8")
            metainfo[b"announce-list"] = [[t.encode("utf-8")] for t in tracker_list]
        seed_list = list(web_seeds)
        if seed_list:
            metainfo[b"url-list"] = [s.encode("utf-8") for s in seed_list]
        if comment:
            metainfo[b"comment"] = comment.encode("utf-8")
        return cls(metainfo)

    # -- properties -------------------------------------------------------

    @property
    def piece_hashes(self) -> list[bytes] | None:
        """SHA-1 hashes for v1/hybrid torrents, ``None`` for v2-only torrents."""
        return self._piece_hashes

    @property
    def has_v1_hashes(self) -> bool:
        return self._piece_hashes is not None

    @property
    def is_hybrid(self) -> bool:
        return self.is_v2 and self._piece_hashes is not None

    @property
    def is_single_file(self) -> bool:
        return b"files" not in self.info and b"file tree" not in self.info

    @property
    def piece_count(self) -> int:
        return len(self._piece_hashes) if self._piece_hashes is not None else 0

    @property
    def metadata_size(self) -> int:
        return len(bencode.encode(self.info))

    @property
    def info_hash_hex(self) -> str:
        return self.info_hash.hex()

    def piece_size(self, index: int) -> int:
        if index < 0 or index >= self.piece_count:
            raise IndexError(f"piece index {index} out of range (0..{self.piece_count - 1})")
        if index != self.piece_count - 1:
            return self.piece_length
        return self.total_length - self.piece_length * (self.piece_count - 1)

    def files_display(self) -> list[str]:
        return [entry.display(self.name, self.is_single_file) for entry in self.files]

    def magnet_uri(self, *, include_web_seeds: bool = True) -> str:
        parts = []
        if self._piece_hashes is not None:
            parts.append(f"xt=urn:btih:{self.info_hash.hex()}")
        if self.info_hash_v2 is not None:
            parts.append(f"xt=urn:btmh:1220{self.info_hash_v2.hex()}")
        parts.append("dn=" + quote(self.name, safe=""))
        parts.append(f"xl={self.total_length}")
        for tracker in self.trackers:
            parts.append("tr=" + quote(tracker, safe=DEFAULT_MAGNET_SAFE))
        if include_web_seeds:
            for seed in self.web_seeds:
                parts.append("ws=" + quote(seed, safe=DEFAULT_MAGNET_SAFE))
        return "magnet:?" + "&".join(parts)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Torrent(name={self.name!r}, size={self.total_length}, "
            f"pieces={self.piece_count}, trackers={len(self.trackers)})"
        )

    # -- internal parsing -------------------------------------------------

    @staticmethod
    def _parse_piece_hashes(info: dict) -> list[bytes] | None:
        pieces = info.get(b"pieces")
        if pieces is None:
            return None
        if not isinstance(pieces, bytes):
            raise MetainfoError("pieces is not a byte string")
        if len(pieces) % PIECE_HASH_SIZE:
            raise MetainfoError("pieces length is not a multiple of 20")
        return [pieces[i : i + PIECE_HASH_SIZE] for i in range(0, len(pieces), PIECE_HASH_SIZE)]

    def _parse_files(self, info: dict) -> list[FileEntry]:
        if b"files" in info:
            raw_files = info[b"files"]
            if not isinstance(raw_files, list) or not raw_files:
                raise MetainfoError("files is not a non-empty list")
            entries: list[FileEntry] = []
            offset = 0
            for position, raw in enumerate(raw_files):
                if not isinstance(raw, dict):
                    raise MetainfoError(f"files[{position}] is not a dictionary")
                length = raw.get(b"length")
                if not isinstance(length, int) or length < 0:
                    raise MetainfoError(f"files[{position}] has no valid length")
                path = _clean_parts(raw.get(b"path"), field=f"files[{position}]")
                entries.append(FileEntry(path, length, offset))
                offset += length
            return entries

        if b"length" in info:
            length = info[b"length"]
            if not isinstance(length, int) or length < 0:
                raise MetainfoError("single-file length is invalid")
            return [FileEntry((), length, 0)]

        if b"file tree" in info:
            return self._parse_file_tree(info[b"file tree"])

        raise MetainfoError("torrent has neither a file list nor a file tree")

    def _parse_file_tree(self, tree: object) -> list[FileEntry]:
        if not isinstance(tree, dict):
            raise MetainfoError("file tree is not a dictionary")
        raw_entries: list[tuple[tuple[str, ...], int]] = []

        def walk(node: object, prefix: list[str]) -> None:
            if not isinstance(node, dict):
                raise MetainfoError("file tree node is not a dictionary")
            leaf = node.get(b"")
            if isinstance(leaf, dict):
                length = leaf.get(b"length")
                if not isinstance(length, int) or length < 0:
                    raise MetainfoError("file tree leaf has no valid length")
                if not prefix:
                    raise MetainfoError("file tree contains an empty path")
                raw_entries.append((tuple(prefix), length))
                return
            for key in sorted(node):
                if not isinstance(key, bytes) or not key:
                    raise MetainfoError("file tree key is invalid")
                walk(node[key], prefix + [_text(key)])

        walk(tree, [])

        entries: list[FileEntry] = []
        offset = 0
        for path, length in raw_entries:
            entries.append(FileEntry(path, length, offset))
            offset += length
        if not entries:
            raise MetainfoError("file tree contains no files")
        return entries

    @staticmethod
    def _parse_trackers(metainfo: dict) -> list[str]:
        urls: list[str] = []
        announce = metainfo.get(b"announce")
        if isinstance(announce, bytes):
            urls.append(_text(announce).strip())
        tiers = metainfo.get(b"announce-list")
        if isinstance(tiers, list):
            for tier in tiers:
                if isinstance(tier, (list, tuple)):
                    for url in tier:
                        if isinstance(url, bytes):
                            urls.append(_text(url).strip())
                elif isinstance(tier, bytes):
                    urls.append(_text(tier).strip())
        result: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url and url not in seen:
                seen.add(url)
                result.append(url)
        return result

    @staticmethod
    def _parse_web_seeds(metainfo: dict) -> list[str]:
        raw = metainfo.get(b"url-list")
        if raw is None:
            return []
        if isinstance(raw, bytes):
            values: Sequence[bytes] = [raw]
        elif isinstance(raw, list):
            values = [item for item in raw if isinstance(item, bytes)]
        else:
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = _text(item).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result


def _decode_btih(value: str) -> bytes:
    value = value.strip()
    if len(value) == 40:
        return bytes.fromhex(value)
    if len(value) == 32:
        return base64.b32decode(value.upper())
    raise ValueError(f"unexpected btih length {len(value)}")


def _decode_btmh(value: str) -> bytes:
    value = value.strip()
    if len(value) == 68 and value[:4].lower() == "1220":
        return bytes.fromhex(value[4:])
    try:
        raw = base64.b32decode(value.upper())
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError("invalid btmh value") from exc
    if len(raw) == 34 and raw[0] == 0x12 and raw[1] == 0x20:
        return raw[2:]
    raise ValueError("unsupported btmh multihash")


@dataclass
class Magnet:
    """A parsed magnet URI."""

    uri: str
    xt: list[str]
    dn: str | None
    trackers: list[str]
    web_seeds: list[str]
    xl: int | None

    @classmethod
    def parse(cls, uri: str) -> "Magnet":
        text = uri.strip()
        lowered = text.lower()
        if not lowered.startswith("magnet:?"):
            raise MetainfoError("not a magnet URI")
        query = text[len("magnet:?") :]
        try:
            params = parse_qs(query, keep_blank_values=True)
        except ValueError as exc:
            raise MetainfoError(f"invalid magnet query: {exc}") from exc

        def first(key: str) -> str | None:
            values = params.get(key)
            return values[0] if values else None

        name = first("dn")
        xl_value = first("xl")
        xl: int | None = None
        if xl_value and xl_value.isdigit():
            xl = int(xl_value)

        return cls(
            uri=text,
            xt=list(params.get("xt", [])),
            dn=name,
            trackers=[t for t in params.get("tr", []) if t],
            web_seeds=[w for w in params.get("ws", []) if w],
            xl=xl,
        )

    @property
    def info_hash(self) -> bytes | None:
        """v1 info hash (btih), or the truncated v2 hash for hybrid magnets."""
        for value in self.xt:
            if value.lower().startswith("urn:btih:"):
                try:
                    return _decode_btih(value[9:])
                except ValueError:
                    continue
        for value in self.xt:
            if value.lower().startswith("urn:btmh:"):
                try:
                    return _decode_btmh(value[9:])[:INFO_HASH_SIZE]
                except ValueError:
                    continue
        return None

    @property
    def info_hash_v2(self) -> bytes | None:
        for value in self.xt:
            if value.lower().startswith("urn:btmh:"):
                try:
                    return _decode_btmh(value[9:])
                except ValueError:
                    continue
        return None

    @property
    def has_v1_hash(self) -> bool:
        return any(v.lower().startswith("urn:btih:") for v in self.xt)

    @property
    def display_name(self) -> str:
        return self.dn or (self.info_hash.hex() if self.info_hash else "unnamed")
