"""On-disk storage for torrent payloads."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Callable, Iterable

from .metainfo import FileEntry, Torrent


class StorageError(RuntimeError):
    pass


class FileStorage:
    """Maps the torrent's flat byte stream onto one or more files.

    Existing files are reused (so downloads can resume); missing files are
    created but *not* preallocated to their final size. Short or missing ranges
    read back as zeroes, which keeps piece verification predictable on a fresh
    directory while avoiding huge upfront disk allocation for large torrents.
    """

    def __init__(self, torrent: Torrent, root: str | os.PathLike[str]):
        self.torrent = torrent
        self.root = Path(root)
        self._handles: dict[Path, object] = {}
        self._lock = threading.RLock()

    # -- layout -----------------------------------------------------------

    def target_path(self, entry: FileEntry) -> Path:
        if self.torrent.is_single_file:
            return self.root / self.torrent.name
        return self.root / self.torrent.name / Path(*entry.path)

    def prepare(
        self,
        progress: Callable[[int, int], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> bool:
        """Create the directory tree and placeholder files without preallocating.

        Returns ``False`` when ``stop_event`` is set before all files are
        touched, so callers can abort a queued download promptly.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.torrent.is_single_file:
            (self.root / self.torrent.name).mkdir(parents=True, exist_ok=True)
        total = len(self.torrent.files)
        for index, entry in enumerate(self.torrent.files, 1):
            if stop_event is not None and stop_event.is_set():
                return False
            path = self.target_path(entry)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch()
            try:
                size = path.stat().st_size
                if size > entry.length:
                    with open(path, "r+b") as handle:
                        handle.truncate(entry.length)
            except OSError as exc:
                raise StorageError(f"cannot prepare {path}: {exc}") from exc
            if progress is not None:
                progress(index, total)
        return True

    # -- I/O --------------------------------------------------------------

    def _handle(self, entry: FileEntry):
        path = self.target_path(entry)
        with self._lock:
            handle = self._handles.get(path)
            if handle is None:
                try:
                    handle = open(path, "r+b", buffering=0)
                except FileNotFoundError:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handle = open(path, "w+b", buffering=0)
                self._handles[path] = handle
            return handle

    def _segments(self, offset: int, length: int) -> Iterable[tuple[FileEntry, int, int]]:
        end = offset + length
        for entry in self.torrent.files:
            if entry.length == 0:
                continue
            start = entry.offset
            stop = entry.offset + entry.length
            if stop <= offset:
                continue
            if start >= end:
                break
            piece_start = max(offset, start)
            piece_stop = min(end, stop)
            if piece_stop > piece_start:
                yield entry, piece_start - start, piece_stop - piece_start

    def write_piece(self, index: int, data: bytes) -> None:
        offset = index * self.torrent.piece_length
        if len(data) != self.torrent.piece_size(index):
            raise StorageError(f"piece {index} has wrong length")
        written = 0
        for entry, file_offset, length in self._segments(offset, len(data)):
            handle = self._handle(entry)
            with self._lock:
                handle.seek(file_offset)
                handle.write(data[written : written + length])
            written += length
        if written != len(data):
            raise StorageError(f"piece {index} did not map completely onto files")

    def read_range(self, offset: int, length: int) -> bytes:
        buffer = bytearray(length)
        for entry, file_offset, segment_length in self._segments(offset, length):
            try:
                handle = self._handle(entry)
                with self._lock:
                    handle.seek(file_offset)
                    chunk = handle.read(segment_length)
            except OSError:
                chunk = b""
            if chunk:
                start = entry.offset + file_offset - offset
                buffer[start : start + len(chunk)] = chunk
        return bytes(buffer)

    def read_piece(self, index: int) -> bytes:
        return self.read_range(index * self.torrent.piece_length, self.torrent.piece_size(index))

    def range_available(
        self,
        offset: int,
        length: int,
        sizes: dict[Path, int] | None = None,
    ) -> bool:
        """Whether a byte range lies entirely inside currently written file data."""
        covered = 0
        for entry, file_offset, segment_length in self._segments(offset, length):
            path = self.target_path(entry)
            try:
                size = sizes[path] if sizes is not None else path.stat().st_size
            except (KeyError, OSError):
                size = 0
            if file_offset + segment_length > size:
                return False
            covered += segment_length
        return covered == length

    def read_block(self, index: int, begin: int, length: int) -> bytes:
        piece_size = self.torrent.piece_size(index)
        if begin < 0 or length <= 0 or begin + length > piece_size:
            raise StorageError("block is outside its piece")
        return self.read_range(index * self.torrent.piece_length + begin, length)

    def verify_all(
        self,
        progress: Callable[[int, int], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[bool]:
        hashes = self.torrent.piece_hashes
        if hashes is None:
            raise StorageError("cannot verify a torrent without v1 piece hashes")
        result = [False] * len(hashes)
        sizes: dict[Path, int] = {}
        for entry in self.torrent.files:
            path = self.target_path(entry)
            try:
                sizes[path] = path.stat().st_size
            except OSError:
                sizes[path] = 0
        for index, expected in enumerate(hashes):
            if stop_event is not None and stop_event.is_set():
                break
            offset = index * self.torrent.piece_length
            size = self.torrent.piece_size(index)
            if self.range_available(offset, size, sizes):
                result[index] = hashlib.sha1(self.read_piece(index)).digest() == expected
            if progress is not None:
                progress(index + 1, len(hashes))
        return result

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except OSError:
                    pass
            self._handles.clear()

    def __enter__(self) -> "FileStorage":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
