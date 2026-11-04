"""BitTorrent peer wire protocol (BEP 3) and metadata exchange (BEP 9/BEP 10)."""

from __future__ import annotations

import hashlib
import ipaddress
import queue
import random
import socket
import string
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import bencode
from .pieces import TorrentState

PSTR = b"BitTorrent protocol"
HANDSHAKE_LEN = 68
BLOCK_SIZE = 16 * 1024
MAX_MESSAGE = 4 * 1024 * 1024
EXTENSION_ID = 20
UT_METADATA = b"ut_metadata"
UT_PEX = b"ut_pex"
# Extension message id we advertise for ut_pex; peers send PEX to us with it.
LOCAL_PEX_ID = 1
METADATA_PIECE_SIZE = 16 * 1024
MAX_METADATA_SIZE = 16 * 1024 * 1024
PEX_INTERVAL = 60.0
# Requests kept in flight per peer. 8 x 16 KiB caps a 200 ms peer at
# ~640 KiB/s; 32 allows ~2.5 MiB/s. Lowered to the peer's advertised reqq.
DEFAULT_PIPELINE = 32
# Pieces one peer may work on at once, so the pipeline does not drain at the
# end of every piece while we wait for the last blocks.
MAX_JOBS_PER_PEER = 4
MAX_PEX_PEERS = 200

# Returned by read_message() for zero-length keep-alives. It must not collide
# with a real id: 0 is CHOKE, and treating keep-alives as chokes stalls peers.
KEEPALIVE = -1
CHOKE = 0
UNCHOKE = 1
INTERESTED = 2
NOT_INTERESTED = 3
HAVE = 4
BITFIELD = 5
REQUEST = 6
PIECE = 7
CANCEL = 8
PORT = 9

_PEER_ID_ALPHABET = string.ascii_letters + string.digits


class ProtocolError(RuntimeError):
    pass


class MetadataError(RuntimeError):
    pass


def make_peer_id(prefix: bytes = b"-TK0100-") -> bytes:
    if len(prefix) != 8:
        raise ValueError("peer id prefix must be 8 bytes")
    suffix = "".join(random.choice(_PEER_ID_ALPHABET) for _ in range(12))
    return prefix + suffix.encode("ascii")


def build_handshake(info_hash: bytes, peer_id: bytes, *, extensions: bool = False) -> bytes:
    if len(info_hash) != 20 or len(peer_id) != 20:
        raise ValueError("info_hash and peer_id must both be 20 bytes")
    reserved = bytearray(8)
    if extensions:
        reserved[5] |= 0x10
    return bytes([len(PSTR)]) + PSTR + bytes(reserved) + info_hash + peer_id


def send_message(sock: socket.socket, message_id: int, payload: bytes = b"") -> None:
    sock.sendall(struct.pack(">IB", len(payload) + 1, message_id) + payload)


def encode_compact_peers(peers: Iterable[tuple[str, int]]) -> tuple[bytes, bytes]:
    """Encode IPv4 and IPv6 peer lists for BEP 11 ``ut_pex``."""
    ipv4 = bytearray()
    ipv6 = bytearray()
    for host, port in peers:
        if not 0 < port < 65536:
            continue
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        packed = address.packed + struct.pack(">H", port)
        if address.version == 4:
            ipv4 += packed
        elif address.version == 6:
            ipv6 += packed
    return bytes(ipv4), bytes(ipv6)


def decode_compact_peers(data: bytes, *, version: int = 4) -> list[tuple[str, int]]:
    """Decode compact peer lists from trackers or BEP 11 ``ut_pex``."""
    if not isinstance(data, (bytes, bytearray)):
        return []
    size = 6 if version == 4 else 18
    family = socket.AF_INET if version == 4 else socket.AF_INET6
    peers: list[tuple[str, int]] = []
    raw = bytes(data)
    for offset in range(0, len(raw) - size + 1, size):
        try:
            host = socket.inet_ntop(family, raw[offset : offset + (4 if version == 4 else 16)])
        except OSError:
            continue
        port = struct.unpack(">H", raw[offset + size - 2 : offset + size])[0]
        if port:
            peers.append((host, port))
        if len(peers) >= MAX_PEX_PEERS:
            break
    return peers


class BufferedSocket:
    """Length-prefixed message reader that tolerates partial TCP reads."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = bytearray()

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("peer closed the connection")
        self.buffer += chunk

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self._fill()
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def read_message(self) -> tuple[int, bytes]:
        while True:
            if len(self.buffer) >= 4:
                length = struct.unpack(">I", self.buffer[:4])[0]
                if length > MAX_MESSAGE:
                    raise ProtocolError(f"peer message too large ({length} bytes)")
                if len(self.buffer) >= 4 + length:
                    payload = bytes(self.buffer[4 : 4 + length])
                    del self.buffer[: 4 + length]
                    if length == 0:
                        return KEEPALIVE, b""
                    return payload[0], payload[1:]
            self._fill()


@dataclass
class _PieceJob:
    index: int
    data: bytearray
    blocks: list[tuple[int, int]]
    received: set[int] = field(default_factory=set)
    next_block: int = 0


class LeechSession(threading.Thread):
    """Downloads pieces from a single peer, sharing state with its siblings."""

    def __init__(
        self,
        state: TorrentState,
        host: str,
        port: int,
        peer_id: bytes,
        stop_event: threading.Event,
        *,
        connect_timeout: float = 8.0,
        idle_timeout: float = 180.0,
        pipeline: int = DEFAULT_PIPELINE,
        on_pex_peers: Callable[[list[tuple[str, int]]], None] | None = None,
        pex_peer_provider: Callable[[], Iterable[tuple[str, int]]] | None = None,
        logger=None,
    ):
        super().__init__(daemon=True, name=f"peer-{host}:{port}")
        self.state = state
        self.host = host
        self.port = port
        self.peer_id = peer_id
        self.stop_event = stop_event
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.pipeline = pipeline
        self.on_pex_peers = on_pex_peers
        self.pex_peer_provider = pex_peer_provider
        self.log = logger

        self.token = id(self)
        self.sock: socket.socket | None = None
        self.buffered: BufferedSocket | None = None
        self.choked = True
        self.choked_since = time.monotonic()
        self.connected = False
        self.ever_received = False
        self._dropped = threading.Event()
        self.error: str | None = None
        self.peer_pieces: set[int] | None = None
        self.peer_pex_id: int | None = None
        self.pex_enabled = on_pex_peers is not None or pex_peer_provider is not None
        self._last_pex = 0.0
        self._pex_sent: set[tuple[str, int]] = set()

        self._jobs: dict[int, _PieceJob] = {}
        self._pending: dict[tuple[int, int], int] = {}
        self._last_activity = 0.0
        self._last_sent = 0.0

    # -- thread body ------------------------------------------------------

    def run(self) -> None:
        try:
            self._run()
        except (OSError, EOFError, ConnectionError, ProtocolError, ValueError) as exc:
            self.error = str(exc) or type(exc).__name__
            if self.log:
                self.log.debug("peer %s:%s stopped: %s", self.host, self.port, self.error)
        finally:
            self._cleanup()

    def close(self) -> None:
        """Stop the whole download (``stop_event`` is shared by all sessions)."""
        self.stop_event.set()
        self.drop()

    def drop(self) -> None:
        """Disconnect only this peer, e.g. to free its slot for a better one."""
        self._dropped.set()
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self) -> None:
        torrent = self.state.torrent
        with socket.create_connection((self.host, self.port), timeout=self.connect_timeout) as sock:
            self.sock = sock
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
            sock.settimeout(10.0)
            self.buffered = BufferedSocket(sock)
            sock.sendall(build_handshake(torrent.info_hash, self.peer_id, extensions=True))
            handshake = self.buffered.read_exact(HANDSHAKE_LEN)
            if handshake[0] != len(PSTR) or handshake[1:HANDSHAKE_LEN - 48] != PSTR:
                raise ProtocolError("peer sent an invalid handshake")
            if handshake[28:48] != torrent.info_hash:
                raise ProtocolError("peer serves a different torrent")
            peer_extensions = bool(handshake[25] & 0x10)
            self.connected = True
            sock.settimeout(1.0)

            self._last_activity = self._last_sent = time.monotonic()
            if peer_extensions:
                self._send_extension_handshake()
            send_message(sock, INTERESTED)

            while (
                not self.stop_event.is_set()
                and not self._dropped.is_set()
                and not self.state.is_complete
            ):
                try:
                    message_id, payload = self.buffered.read_message()
                except socket.timeout:
                    now = time.monotonic()
                    if now - self._last_activity > self.idle_timeout:
                        raise ProtocolError("peer became idle")
                    if now - self._last_sent > 60:
                        sock.sendall(b"\x00\x00\x00\x00")
                        self._last_sent = now
                    self._maybe_send_pex(now)
                    self._pump()
                    continue
                self._last_activity = time.monotonic()
                self._handle(message_id, payload)
                # A busy peer may never let the read time out, so check here too.
                self._maybe_send_pex(self._last_activity)

    # -- protocol ---------------------------------------------------------

    def _handle(self, message_id: int, payload: bytes) -> None:
        if message_id == CHOKE:
            if not self.choked:
                self.choked_since = time.monotonic()
            self.choked = True
            # BEP 3: a choking peer discards our outstanding requests. Hand
            # the pieces (and their received blocks) back so others finish them.
            self._pending.clear()
            self._release_jobs()
        elif message_id == UNCHOKE:
            self.choked = False
            self._pump()
        elif message_id == HAVE:
            if len(payload) < 4:
                raise ProtocolError("short have message")
            index = struct.unpack(">I", payload[:4])[0]
            if self.peer_pieces is None:
                self.peer_pieces = set()
            self.peer_pieces.add(index)
            self.state.register_peer(self.token, frozenset(self.peer_pieces))
            self._pump()
        elif message_id == BITFIELD:
            self._handle_bitfield(payload)
        elif message_id == PIECE:
            self._handle_piece(payload)
        elif message_id == EXTENSION_ID:
            self._handle_extension(payload)

    def _send_extension_handshake(self) -> None:
        if self.sock is None:
            return
        mapping = {UT_PEX: LOCAL_PEX_ID} if self.pex_enabled else {}
        try:
            _send_extension(
                self.sock,
                0,
                bencode.encode({b"m": mapping, b"v": b"torrentkit/0.1.0"}),
            )
        except OSError:
            pass

    def _handle_extension(self, payload: bytes) -> None:
        if not payload:
            return
        extension_id, body = payload[0], payload[1:]
        if extension_id == 0:
            try:
                handshake = bencode.decode(body, strict=False)
            except bencode.BencodeError:
                return
            if not isinstance(handshake, dict):
                return
            reqq = handshake.get(b"reqq")
            if isinstance(reqq, int) and reqq > 0:
                self.pipeline = max(1, min(self.pipeline, reqq))
            mapping = handshake.get(b"m")
            if isinstance(mapping, dict):
                raw_id = mapping.get(UT_PEX)
                if self.pex_enabled and isinstance(raw_id, int) and 0 < raw_id < 256:
                    self.peer_pex_id = raw_id
                    self._send_pex()
                elif raw_id == 0:
                    self.peer_pex_id = None
            return

        # Incoming messages carry the id *we* advertised, not the peer's id.
        if not self.pex_enabled or extension_id != LOCAL_PEX_ID:
            return
        try:
            message = bencode.decode(body, strict=False)
        except bencode.BencodeError:
            return
        if not isinstance(message, dict) or self.on_pex_peers is None:
            return

        peers: list[tuple[str, int]] = []
        added = message.get(b"added")
        if isinstance(added, bytes):
            peers.extend(decode_compact_peers(added, version=4))
        added6 = message.get(b"added6")
        if isinstance(added6, bytes):
            peers.extend(decode_compact_peers(added6, version=6))
        if peers:
            try:
                self.on_pex_peers(peers)
            except Exception:
                if self.log:
                    self.log.debug("pex peer callback failed", exc_info=True)

    def _maybe_send_pex(self, now: float) -> None:
        if self.peer_pex_id is not None and now - self._last_pex > PEX_INTERVAL:
            self._send_pex()

    def _send_pex(self) -> None:
        if self.sock is None or self.peer_pex_id is None or self.pex_peer_provider is None:
            return
        # Throttle even when there is nothing new, so idle loops stay cheap.
        self._last_pex = time.monotonic()
        try:
            known = list(self.pex_peer_provider())
        except Exception:
            known = []
        # BEP 11 messages are deltas: only advertise peers this peer has not seen.
        others = [
            (host, port)
            for host, port in dict.fromkeys(known)
            if (host, port) != (self.host, self.port) and (host, port) not in self._pex_sent
        ][:MAX_PEX_PEERS]
        if not others:
            return
        ipv4, ipv6 = encode_compact_peers(others)
        payload: dict = {}
        if ipv4:
            payload[b"added"] = ipv4
            payload[b"added.f"] = b"\x00" * (len(ipv4) // 6)
        if ipv6:
            payload[b"added6"] = ipv6
            payload[b"added6.f"] = b"\x00" * (len(ipv6) // 18)
        if not payload:
            return
        try:
            _send_extension(self.sock, self.peer_pex_id, bencode.encode(payload))
        except OSError:
            return
        self._pex_sent.update(others)

    def _handle_bitfield(self, payload: bytes) -> None:
        count = self.state.piece_count
        expected = (count + 7) // 8
        if len(payload) < expected:
            raise ProtocolError("short bitfield message")
        pieces = {
            index
            for index in range(count)
            if payload[index // 8] & (0x80 >> (index % 8))
        }
        self.peer_pieces = pieces
        self.state.register_peer(self.token, frozenset(pieces))
        self._pump()

    def _handle_piece(self, payload: bytes) -> None:
        if len(payload) < 8:
            raise ProtocolError("short piece message")
        index, begin = struct.unpack(">II", payload[:8])
        block = payload[8:]
        job = self._jobs.get(index)
        if job is None:
            return
        expected = self._pending.pop((index, begin), None)
        if expected is None:
            return
        if len(block) != expected:
            raise ProtocolError("peer sent a block with the wrong length")
        job.data[begin : begin + len(block)] = block
        job.received.add(begin)
        self.ever_received = True
        self.state.add_received(len(block))
        if len(job.received) == len(job.blocks):
            del self._jobs[index]
            if not self.state.store(index, bytes(job.data), self.token):
                self.state.release(index, self.token)
                raise ProtocolError(f"piece {index} failed the hash check")
        # Keep the pipeline full: request the next block (or piece) right away,
        # otherwise pieces with more blocks than the pipeline depth stall.
        self._pump()

    def _pump(self) -> None:
        if self.choked or self.stop_event.is_set() or self.state.is_complete:
            return
        if self.peer_pieces is None or self.sock is None:
            return
        while len(self._pending) < self.pipeline:
            job = next(
                (job for job in self._jobs.values() if job.next_block < len(job.blocks)),
                None,
            )
            if job is None:
                if len(self._jobs) >= MAX_JOBS_PER_PEER:
                    return
                job = self._start_job()
                if job is None:
                    return
            begin, length = job.blocks[job.next_block]
            job.next_block += 1
            if begin in job.received or (job.index, begin) in self._pending:
                continue
            send_message(self.sock, REQUEST, struct.pack(">III", job.index, begin, length))
            self._pending[(job.index, begin)] = length
            self._last_sent = time.monotonic()

    def _start_job(self) -> _PieceJob | None:
        index = self.state.acquire(self.token, self.peer_pieces)
        if index is None:
            return None
        size = self.state.torrent.piece_size(index)
        blocks = [
            (begin, min(BLOCK_SIZE, size - begin))
            for begin in range(0, size, BLOCK_SIZE)
        ]
        saved = self.state.take_partial(index, self.token)
        if saved is not None and len(saved[0]) == size:
            data, received = saved
        else:
            data, received = bytearray(size), set()
        job = _PieceJob(index=index, data=data, blocks=blocks, received=received)
        self._jobs[index] = job
        return job

    def _release_jobs(self) -> None:
        jobs, self._jobs = self._jobs, {}
        for job in jobs.values():
            self.state.release(job.index, self.token, job.data, job.received)

    @property
    def job_count(self) -> int:
        return len(self._jobs)

    def _cleanup(self) -> None:
        self._release_jobs()
        self.state.unregister_peer(self.token)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.buffered = None


# -- metadata exchange (BEP 9 / BEP 10) ------------------------------------


def _send_extension(sock: socket.socket, extension_id: int, body: bytes) -> None:
    send_message(sock, EXTENSION_ID, bytes([extension_id]) + body)


def _request_metadata_pieces(
    sock: socket.socket, peer_meta_id: int, metadata_size: int, requested: set[int]
) -> None:
    total = (metadata_size + METADATA_PIECE_SIZE - 1) // METADATA_PIECE_SIZE
    for index in range(total):
        if index not in requested:
            requested.add(index)
            body = bencode.encode({b"msg_type": 0, b"piece": index})
            _send_extension(sock, peer_meta_id, body)


def _fetch_metadata_from_peer(
    info_hash: bytes,
    address: tuple[str, int],
    peer_id: bytes,
    timeout: float,
    stop: threading.Event,
) -> dict:
    host, port = address
    with socket.create_connection((host, port), timeout=min(6.0, timeout)) as sock:
        sock.settimeout(10.0)
        buffered = BufferedSocket(sock)
        sock.sendall(build_handshake(info_hash, peer_id, extensions=True))
        handshake = buffered.read_exact(HANDSHAKE_LEN)
        if handshake[28:48] != info_hash:
            raise MetadataError("peer serves a different torrent")

        _send_extension(
            sock,
            0,
            bencode.encode({b"m": {UT_METADATA: 1}, b"v": b"torrentkit/0.1.0"}),
        )
        sock.settimeout(1.0)

        peer_meta_id: int | None = None
        metadata_size: int | None = None
        chunks: dict[int, bytes] = {}
        requested: set[int] = set()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if stop.is_set():
                raise MetadataError("metadata fetch was cancelled")
            try:
                message_id, payload = buffered.read_message()
            except socket.timeout:
                continue
            if message_id != EXTENSION_ID or not payload:
                continue
            extension_id, body = payload[0], payload[1:]

            if extension_id == 0:
                try:
                    handshake_dict = bencode.decode(body, strict=False)
                except bencode.BencodeError:
                    continue
                if not isinstance(handshake_dict, dict):
                    continue
                mapping = handshake_dict.get(b"m")
                if isinstance(mapping, dict) and UT_METADATA in mapping:
                    peer_meta_id = int(mapping[UT_METADATA])
                size = handshake_dict.get(b"metadata_size")
                if isinstance(size, int) and 0 < size <= MAX_METADATA_SIZE:
                    metadata_size = size
                if peer_meta_id is not None:
                    if metadata_size is not None:
                        _request_metadata_pieces(sock, peer_meta_id, metadata_size, requested)
                    elif 0 not in requested:
                        requested.add(0)
                        _send_extension(
                            sock, peer_meta_id, bencode.encode({b"msg_type": 0, b"piece": 0})
                        )
                continue

            if peer_meta_id is None or extension_id != peer_meta_id:
                continue
            try:
                header, offset = bencode.decode_prefix(body)
            except bencode.BencodeError:
                continue
            if not isinstance(header, dict):
                continue
            message_type = header.get(b"msg_type")
            if message_type == 2:
                raise MetadataError("peer rejected the metadata request")
            if message_type != 1:
                continue
            piece = header.get(b"piece")
            if not isinstance(piece, int) or piece < 0 or piece > MAX_METADATA_SIZE // METADATA_PIECE_SIZE:
                continue
            total_size = header.get(b"total_size")
            if isinstance(total_size, int) and 0 < total_size <= MAX_METADATA_SIZE:
                metadata_size = total_size
            chunks[piece] = body[offset:]
            if metadata_size is None:
                continue
            needed = (metadata_size + METADATA_PIECE_SIZE - 1) // METADATA_PIECE_SIZE
            if len(chunks) < needed or any(index not in chunks for index in range(needed)):
                _request_metadata_pieces(sock, peer_meta_id, metadata_size, requested)
                continue
            blob = b"".join(chunks[index] for index in range(needed))[:metadata_size]
            if hashlib.sha1(blob).digest() != info_hash:
                raise MetadataError("metadata failed the info hash check")
            try:
                info = bencode.decode(blob, strict=False)
            except bencode.BencodeError as exc:
                raise MetadataError(f"metadata is not valid bencode: {exc}") from exc
            if not isinstance(info, dict):
                raise MetadataError("metadata is not an info dictionary")
            return info

    raise MetadataError("metadata request timed out")


def fetch_metadata(
    info_hash: bytes,
    peers: Iterable[tuple[str, int]],
    peer_id: bytes,
    *,
    timeout: float = 12.0,
    max_peers: int = 30,
    logger=None,
) -> dict:
    """Fetch the info dictionary for ``info_hash`` using ut_metadata.

    Peers are queried in parallel; the first valid response wins.
    """
    candidates = list(dict.fromkeys(peers))[:max_peers]
    if not candidates:
        raise MetadataError("no peers available to fetch metadata from")

    results: queue.Queue = queue.Queue()
    stop = threading.Event()

    def attempt(address: tuple[str, int]) -> None:
        if stop.is_set():
            return
        try:
            info = _fetch_metadata_from_peer(info_hash, address, peer_id, timeout, stop)
            results.put((address, info, None))
        except Exception as exc:  # reported to the caller below
            results.put((address, None, exc))

    threads = [threading.Thread(target=attempt, args=(peer,), daemon=True) for peer in candidates]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout + 5.0
    failures: list[BaseException] = []
    while time.monotonic() < deadline:
        try:
            address, info, error = results.get(timeout=0.5)
        except queue.Empty:
            if not any(thread.is_alive() for thread in threads):
                break
            continue
        if info is not None:
            stop.set()
            return info
        if error is not None:
            failures.append(error)
            if logger:
                logger.debug("metadata from %s:%s failed: %s", address[0], address[1], error)
        if results.empty() and not any(thread.is_alive() for thread in threads):
            break

    stop.set()
    detail = f" (last error: {failures[-1]})" if failures else ""
    raise MetadataError(f"could not fetch metadata from {len(candidates)} peer(s){detail}")
