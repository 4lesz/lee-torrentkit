"""Piece bookkeeping shared by all peer connections."""

from __future__ import annotations

import hashlib
import threading
from typing import Callable, Iterable

from .metainfo import Torrent
from .storage import FileStorage

# Upper bound on block data kept from abandoned pieces. Large torrents use
# 16 MiB pieces, so dropping partial data on every disconnect would mean a
# flaky swarm never finishes a single piece.
MAX_PARTIAL_BYTES = 256 * 1024 * 1024


class TorrentState:
    """Tracks verified, in-flight and available pieces for one torrent."""

    def __init__(self, torrent: Torrent, storage: FileStorage, *, rarest_first: bool = False):
        if not torrent.has_v1_hashes:
            raise ValueError("torrents without v1 piece hashes cannot be downloaded by this client")
        self.torrent = torrent
        self.storage = storage
        self.rarest_first = rarest_first
        self.piece_count = torrent.piece_count

        self._done = [False] * self.piece_count
        self._done_bytes = 0
        self._inflight: dict[int, int] = {}
        self._availability = [0] * self.piece_count
        self._peer_pieces: dict[int, frozenset[int]] = {}
        self._partials: dict[int, tuple[bytearray, set[int]]] = {}
        self._partial_bytes = 0
        self._received = 0
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    def load_existing(
        self,
        progress: Callable[[int, int], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        if self.piece_count == 0:
            return
        verified = self.storage.verify_all(progress, stop_event)
        with self._lock:
            self._done = verified
            self._done_bytes = sum(
                self.torrent.piece_size(index)
                for index, ok in enumerate(verified)
                if ok
            )

    # -- peer availability ------------------------------------------------

    def register_peer(self, token: int, pieces: frozenset[int]) -> None:
        with self._lock:
            previous = self._peer_pieces.pop(token, None)
            if previous:
                for index in previous:
                    self._availability[index] = max(0, self._availability[index] - 1)
            self._peer_pieces[token] = pieces
            for index in pieces:
                if 0 <= index < self.piece_count:
                    self._availability[index] += 1

    def unregister_peer(self, token: int) -> None:
        with self._lock:
            previous = self._peer_pieces.pop(token, None)
            if previous:
                for index in previous:
                    self._availability[index] = max(0, self._availability[index] - 1)

    # -- scheduling -------------------------------------------------------

    def acquire(self, owner: int, peer_pieces: Iterable[int] | None = None) -> int | None:
        available = None if peer_pieces is None else frozenset(peer_pieces)
        with self._lock:
            if all(self._done):
                return None
            # Finish pieces another peer started before opening new ones.
            for index in self._partials:
                if index in self._inflight or self._done[index]:
                    continue
                if available is not None and index not in available:
                    continue
                self._inflight[index] = owner
                return index
            best = -1
            best_availability = 0
            for index in range(self.piece_count):
                if self._done[index] or index in self._inflight:
                    continue
                if available is not None and index not in available:
                    continue
                if not self.rarest_first:
                    best = index
                    break
                count = self._availability[index]
                if best < 0 or count < best_availability:
                    best = index
                    best_availability = count
            if best < 0:
                return None
            self._inflight[best] = owner
            return best

    def release(
        self,
        index: int,
        owner: int,
        data: bytearray | None = None,
        received: Iterable[int] = (),
    ) -> None:
        """Give a piece back; blocks already received are kept for the next peer."""
        blocks = set(received)
        with self._lock:
            if self._inflight.get(index) != owner:
                return
            del self._inflight[index]
            if data is None or not blocks or self._done[index]:
                return
            self._drop_partial(index)
            self._partials[index] = (data, blocks)
            self._partial_bytes += len(data)
            while self._partial_bytes > MAX_PARTIAL_BYTES and len(self._partials) > 1:
                # Evict the piece with the least progress (never the one just saved).
                victim = min(
                    (key for key in self._partials if key != index),
                    key=lambda key: len(self._partials[key][1]),
                )
                self._drop_partial(victim)

    def take_partial(self, index: int, owner: int) -> tuple[bytearray, set[int]] | None:
        """Hand the saved blocks of an acquired piece to its new owner."""
        with self._lock:
            if self._inflight.get(index) != owner:
                return None
            entry = self._partials.pop(index, None)
            if entry is not None:
                self._partial_bytes -= len(entry[0])
            return entry

    def add_received(self, count: int) -> None:
        with self._lock:
            self._received += count

    def _drop_partial(self, index: int) -> None:
        entry = self._partials.pop(index, None)
        if entry is not None:
            self._partial_bytes -= len(entry[0])

    def store(self, index: int, data: bytes, owner: int | None = None) -> bool:
        """Verify a completed piece, write it and mark it done."""
        expected = self.torrent.piece_hashes
        if expected is None or index < 0 or index >= self.piece_count:
            return False
        if len(data) != self.torrent.piece_size(index):
            return False
        if hashlib.sha1(data).digest() != expected[index]:
            return False
        self.storage.write_piece(index, data)
        with self._lock:
            if not self._done[index]:
                self._done[index] = True
                self._done_bytes += len(data)
            self._inflight.pop(index, None)
            self._drop_partial(index)
        return True

    # -- reporting --------------------------------------------------------

    @property
    def downloaded(self) -> int:
        with self._lock:
            return self._done_bytes

    @property
    def received(self) -> int:
        """Payload bytes received from peers, including unfinished pieces."""
        with self._lock:
            return self._received

    @property
    def is_complete(self) -> bool:
        with self._lock:
            return self.piece_count == 0 or all(self._done)

    @property
    def pieces_done(self) -> int:
        with self._lock:
            return sum(1 for flag in self._done if flag)

    def availability(self, index: int) -> int:
        with self._lock:
            return self._availability[index]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "downloaded": self._done_bytes,
                "total": self.torrent.total_length,
                "pieces_done": sum(1 for flag in self._done if flag),
                "pieces_total": self.piece_count,
                "inflight": len(self._inflight),
                "received": self._received,
                "partial_pieces": len(self._partials),
                "peers_with_pieces": len(self._peer_pieces),
            }
