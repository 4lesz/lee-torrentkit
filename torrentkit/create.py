"""Creation of v1 ``.torrent`` files from local files and directories."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Iterable

from . import bencode
from .metainfo import Torrent

MIN_PIECE_LENGTH = 16 * 1024
MAX_PIECE_LENGTH = 64 * 1024 * 1024
READ_CHUNK = 1024 * 1024


class CreateError(ValueError):
    pass


def _validate_piece_length(piece_length: int) -> None:
    if piece_length < MIN_PIECE_LENGTH or piece_length > MAX_PIECE_LENGTH:
        raise CreateError(
            f"piece length must be between {MIN_PIECE_LENGTH} and {MAX_PIECE_LENGTH} bytes"
        )
    if piece_length & (piece_length - 1):
        raise CreateError("piece length must be a power of two")


def _collect(path: Path) -> tuple[bool, list[tuple[tuple[str, ...], Path]]]:
    if path.is_file():
        return True, [((), path)]
    if not path.is_dir():
        raise CreateError(f"{path} is neither a file nor a directory")
    entries: list[tuple[tuple[str, ...], Path]] = []
    for candidate in sorted(path.rglob("*")):
        if candidate.is_file():
            entries.append((candidate.relative_to(path).parts, candidate))
    if not entries:
        raise CreateError(f"{path} contains no files")
    return False, entries


def create_torrent(
    path: str | os.PathLike[str],
    *,
    piece_length: int = 256 * 1024,
    trackers: Iterable[str] = (),
    private: bool = False,
    comment: str | None = None,
    created_by: str = "torrentkit 0.1.0",
    name: str | None = None,
) -> dict:
    """Build a v1 metainfo dictionary for ``path``.

    The returned dictionary is ready to be bencoded and written to disk, or
    passed to :class:`torrentkit.metainfo.Torrent`.
    """
    _validate_piece_length(piece_length)
    source = Path(path)
    if not source.exists():
        raise CreateError(f"{source} does not exist")
    single_file, entries = _collect(source)

    torrent_name = name or source.name
    if not torrent_name:
        raise CreateError("torrent name is empty")

    records = []
    pieces = bytearray()
    buffer = bytearray()
    total = 0

    for parts, file_path in entries:
        file_length = file_path.stat().st_size
        records.append({"length": file_length, "path": list(parts)})
        total += file_length
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(READ_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= piece_length:
                    pieces += hashlib.sha1(buffer[:piece_length]).digest()
                    del buffer[:piece_length]
    if buffer:
        pieces += hashlib.sha1(bytes(buffer)).digest()

    info: dict = {
        b"name": torrent_name.encode("utf-8"),
        b"piece length": piece_length,
        b"pieces": bytes(pieces),
    }
    if single_file:
        info[b"length"] = total
    else:
        info[b"files"] = [
            {
                b"length": record["length"],
                b"path": [p.encode("utf-8") for p in record["path"]],
            }
            for record in records
        ]
    if private:
        info[b"private"] = 1

    metainfo: dict = {
        b"info": info,
        b"creation date": int(time.time()),
        b"created by": created_by.encode("utf-8"),
    }
    tracker_list = [t for t in trackers if t]
    if tracker_list:
        metainfo[b"announce"] = tracker_list[0].encode("utf-8")
        metainfo[b"announce-list"] = [[t.encode("utf-8")] for t in tracker_list]
    if comment:
        metainfo[b"comment"] = comment.encode("utf-8")

    # Fail early if we produced something incoherent.
    Torrent(metainfo)
    return metainfo


def write_torrent(metainfo: dict, destination: str | os.PathLike[str]) -> None:
    with open(destination, "wb") as handle:
        handle.write(bencode.encode(metainfo))
