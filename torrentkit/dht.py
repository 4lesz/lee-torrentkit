"""A small BitTorrent DHT node (BEP 5).

The implementation focuses on what a downloader needs: bootstrap into the
network, look up peers for one info hash, announce itself, and answer the
basic queries other nodes send. It is not a full routing-table implementation
and only speaks IPv4.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import queue
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from . import bencode
from .peers import decode_compact_peers
from .sockets import configure_reuse

DEFAULT_BOOTSTRAP = (
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
)
K = 8
MAX_NODES = 200
MAX_STORED_PEERS = 200
QUERY_TIMEOUT = 5.0
DEFAULT_QUERY_INTERVAL = 30.0
DEFAULT_ANNOUNCE_INTERVAL = 300.0


@dataclass
class _Node:
    node_id: bytes
    host: str
    port: int
    last_seen: float


class DHTNode:
    """One DHT participant for a single info hash."""

    def __init__(
        self,
        info_hash: bytes,
        *,
        port: int = 0,
        announce_port: int = 6881,
        bootstrap: Iterable[tuple[str, int]] = DEFAULT_BOOTSTRAP,
        on_peers: Callable[[list[tuple[str, int]]], None] | None = None,
        query_interval: float = DEFAULT_QUERY_INTERVAL,
        announce_interval: float = DEFAULT_ANNOUNCE_INTERVAL,
        logger=None,
    ):
        if not isinstance(info_hash, bytes) or len(info_hash) != 20:
            raise ValueError("info_hash must be 20 bytes")
        self.info_hash = info_hash
        self.node_id = os.urandom(20)
        self.announce_port = announce_port
        self.bootstrap = list(bootstrap)
        self.on_peers = on_peers
        self.query_interval = query_interval
        self.announce_interval = announce_interval
        self.logger = logger or logging.getLogger("torrentkit")

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        configure_reuse(self._sock)
        try:
            self._sock.bind(("0.0.0.0", port))
        except OSError:
            self._sock.bind(("0.0.0.0", 0))
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]

        self._nodes: dict[bytes, _Node] = {}
        self._pending: dict[bytes, tuple[float, Callable | None]] = {}
        self._tokens: dict[tuple[str, int], bytes] = {}
        self._last_announce: dict[tuple[str, int], float] = {}
        self._announced: dict[bytes, list[tuple[str, int]]] = {}
        self._seen_peers: set[tuple[str, int]] = set()
        self._peer_queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._secret = os.urandom(16)
        self._tid = random.getrandbits(16)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "DHTNode":
        if self._thread is not None:
            raise RuntimeError("DHT node is already running")
        self._thread = threading.Thread(
            target=self._loop, name="torrentkit-dht", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "DHTNode":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- public helpers ---------------------------------------------------

    def add_node(self, node_id: bytes, host: str, port: int) -> None:
        if not isinstance(node_id, bytes) or len(node_id) != 20:
            return
        if not 0 < port < 65536 or node_id == self.node_id:
            return
        with self._lock:
            self._nodes[node_id] = _Node(node_id, host, port, time.monotonic())
            if len(self._nodes) > MAX_NODES:
                keep = sorted(
                    self._nodes.values(), key=lambda node: node.last_seen, reverse=True
                )[:MAX_NODES]
                self._nodes = {node.node_id: node for node in keep}

    def get_peers(self) -> int:
        nodes = self._closest(self.info_hash, K)
        for node in nodes:
            self._send_query(
                node.host,
                node.port,
                b"get_peers",
                {b"id": self.node_id, b"info_hash": self.info_hash},
            )
        return len(nodes)

    def announce(self) -> int:
        with self._lock:
            tokens = list(self._tokens.items())
        count = 0
        for (host, port), token in tokens:
            self._announce_to((host, port), token)
            count += 1
        return count

    def collect_peers(self, timeout: float) -> list[tuple[str, int]]:
        deadline = time.monotonic() + timeout
        peers: list[tuple[str, int]] = []
        while time.monotonic() < deadline:
            try:
                peer = self._peer_queue.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if peer not in peers:
                peers.append(peer)
        return peers

    def publish_peer(self, peer: tuple[str, int]) -> None:
        """Store a peer locally (useful for tests and small private swarms)."""
        self._store_peer(self.info_hash, peer)

    @property
    def node_count(self) -> int:
        with self._lock:
            return len(self._nodes)

    # -- event loop -------------------------------------------------------

    def _loop(self) -> None:
        self._bootstrap()
        next_query = time.monotonic() + 1.0
        next_announce = time.monotonic() + self.announce_interval
        while not self._stop.is_set():
            try:
                data, address = self._sock.recvfrom(65535)
            except socket.timeout:
                data = None
                address = None
            except OSError:
                break
            if data is not None and address is not None:
                self._handle_datagram(data, address)

            now = time.monotonic()
            self._prune_pending(now)
            if now >= next_query:
                if self._nodes:
                    self.get_peers()
                else:
                    self._bootstrap()
                next_query = now + self.query_interval
            if now >= next_announce:
                self.announce()
                next_announce = now + self.announce_interval

    def _bootstrap(self) -> None:
        for host, port in self.bootstrap:
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
            except OSError:
                continue
            for info in infos[:2]:
                address = info[4][:2]
                self._send_query(
                    address[0],
                    address[1],
                    b"find_node",
                    {b"id": self.node_id, b"target": self.node_id},
                )

    def _handle_datagram(self, data: bytes, address: tuple[str, int]) -> None:
        try:
            message = bencode.decode(data, strict=False)
        except bencode.BencodeError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get(b"y")
        if kind == b"q":
            self._handle_query(message, address)
        elif kind in (b"r", b"e"):
            self._handle_response(message, address)

    def _handle_query(self, message: dict, address: tuple[str, int]) -> None:
        transaction = message.get(b"t")
        query = message.get(b"q")
        args = message.get(b"a")
        if not isinstance(transaction, bytes) or not isinstance(query, bytes):
            return
        if not isinstance(args, dict):
            return
        raw_id = args.get(b"id")
        if isinstance(raw_id, bytes) and len(raw_id) == 20:
            self.add_node(raw_id, address[0], address[1])

        if query == b"ping":
            self._send_response(address, transaction, {b"id": self.node_id})
            return

        if query == b"find_node":
            target = args.get(b"target")
            if not isinstance(target, bytes) or len(target) != 20:
                target = self.node_id
            self._send_response(
                address,
                transaction,
                {
                    b"id": self.node_id,
                    b"nodes": self._compact_nodes(self._closest(target, K)),
                },
            )
            return

        if query == b"get_peers":
            info_hash = args.get(b"info_hash")
            if not isinstance(info_hash, bytes) or len(info_hash) != 20:
                self._send_error(address, transaction, 203, "invalid info_hash")
                return
            response: dict = {
                b"id": self.node_id,
                b"token": self._make_token(address[0]),
                b"nodes": self._compact_nodes(self._closest(info_hash, K)),
            }
            peers = self._stored_peers(info_hash)
            if peers:
                compact = [self._compact_peer(peer) for peer in peers]
                response[b"values"] = [item for item in compact if item is not None]
            self._send_response(address, transaction, response)
            return

        if query == b"announce_peer":
            info_hash = args.get(b"info_hash")
            token = args.get(b"token")
            if (
                not isinstance(info_hash, bytes)
                or len(info_hash) != 20
                or not isinstance(token, bytes)
                or not self._valid_token(address[0], token)
            ):
                self._send_error(address, transaction, 203, "bad token")
                return
            implied = args.get(b"implied_port")
            raw_port = args.get(b"port")
            if implied == 1:
                peer_port = address[1]
            elif isinstance(raw_port, int) and 0 < raw_port < 65536:
                peer_port = raw_port
            else:
                self._send_error(address, transaction, 203, "invalid port")
                return
            self._store_peer(info_hash, (address[0], peer_port))
            self._send_response(address, transaction, {b"id": self.node_id})
            return

        self._send_error(address, transaction, 204, "unknown query")

    def _handle_response(self, message: dict, address: tuple[str, int]) -> None:
        transaction = message.get(b"t")
        if not isinstance(transaction, bytes):
            return
        with self._lock:
            pending = self._pending.pop(transaction, None)
        # Ignore unsolicited or late replies so nobody can inject nodes/peers.
        if pending is None:
            return
        response = message.get(b"r")
        if not isinstance(response, dict):
            return

        raw_id = response.get(b"id")
        if isinstance(raw_id, bytes) and len(raw_id) == 20:
            self.add_node(raw_id, address[0], address[1])

        self._absorb_nodes(response.get(b"nodes"))
        self._absorb_nodes(response.get(b"nodes6"), ipv6=True)

        values = response.get(b"values")
        if isinstance(values, list):
            peers: list[tuple[str, int]] = []
            for item in values:
                if isinstance(item, bytes):
                    peers.extend(decode_compact_peers(item))
            if peers:
                self._emit_peers(peers)
        elif isinstance(values, bytes):
            self._emit_peers(decode_compact_peers(values))

        token = response.get(b"token")
        if isinstance(token, bytes) and token:
            peer_key = (address[0], address[1])
            with self._lock:
                self._tokens[peer_key] = token
                last = self._last_announce.get(peer_key, 0.0)
            if time.monotonic() - last >= self.announce_interval:
                self._announce_to(peer_key, token)

    # -- queries ----------------------------------------------------------

    def _send_query(
        self,
        host: str,
        port: int,
        query: bytes,
        args: dict,
        callback: Callable | None = None,
    ) -> None:
        if self._stop.is_set():
            return
        with self._lock:
            self._tid = (self._tid + 1) & 0xFFFF
            transaction = struct.pack(">H", self._tid)
            self._pending[transaction] = (time.monotonic() + QUERY_TIMEOUT, callback)
        message = {
            b"t": transaction,
            b"y": b"q",
            b"q": query,
            b"a": args,
        }
        try:
            self._sock.sendto(bencode.encode(message), (host, port))
        except OSError:
            with self._lock:
                self._pending.pop(transaction, None)

    def _send_response(
        self, address: tuple[str, int], transaction: bytes, response: dict
    ) -> None:
        message = {b"t": transaction, b"y": b"r", b"r": response}
        try:
            self._sock.sendto(bencode.encode(message), address)
        except OSError:
            pass

    def _send_error(
        self, address: tuple[str, int], transaction: bytes, code: int, text: str
    ) -> None:
        message = {
            b"t": transaction,
            b"y": b"e",
            b"e": [code, text.encode("utf-8", "replace")],
        }
        try:
            self._sock.sendto(bencode.encode(message), address)
        except OSError:
            pass

    def _announce_to(self, address: tuple[str, int], token: bytes) -> None:
        self._send_query(
            address[0],
            address[1],
            b"announce_peer",
            {
                b"id": self.node_id,
                b"info_hash": self.info_hash,
                b"port": self.announce_port,
                b"token": token,
                b"implied_port": 0,
            },
        )
        with self._lock:
            self._last_announce[address] = time.monotonic()

    # -- helpers ----------------------------------------------------------

    def _prune_pending(self, now: float) -> None:
        with self._lock:
            expired = [
                transaction
                for transaction, (deadline, _callback) in self._pending.items()
                if deadline < now
            ]
            for transaction in expired:
                self._pending.pop(transaction, None)

    def _closest(self, target: bytes, count: int) -> list[_Node]:
        with self._lock:
            nodes = list(self._nodes.values())
        wanted = int.from_bytes(target, "big")

        def distance(node: _Node) -> int:
            return int.from_bytes(node.node_id, "big") ^ wanted

        return sorted(nodes, key=distance)[:count]

    def _absorb_nodes(self, data: object, *, ipv6: bool = False) -> None:
        if not isinstance(data, bytes) or not data:
            return
        size = 38 if ipv6 else 26
        raw = bytes(data)
        for offset in range(0, len(raw) - size + 1, size):
            chunk = raw[offset : offset + size]
            node_id = chunk[:20]
            if ipv6:
                continue
            host = socket.inet_ntoa(chunk[20:24])
            port = struct.unpack(">H", chunk[24:26])[0]
            self.add_node(node_id, host, port)

    def _compact_nodes(self, nodes: Iterable[_Node]) -> bytes:
        out = bytearray()
        for node in nodes:
            try:
                out += node.node_id + socket.inet_aton(node.host) + struct.pack(">H", node.port)
            except OSError:
                continue
        return bytes(out)

    @staticmethod
    def _compact_peer(peer: tuple[str, int]) -> bytes | None:
        host, port = peer
        try:
            return socket.inet_aton(host) + struct.pack(">H", port)
        except (OSError, struct.error):
            return None

    def _emit_peers(self, peers: Iterable[tuple[str, int]]) -> None:
        fresh: list[tuple[str, int]] = []
        with self._lock:
            for peer in peers:
                if peer in self._seen_peers:
                    continue
                self._seen_peers.add(peer)
                fresh.append(peer)
            if len(self._seen_peers) > 2000:
                self._seen_peers.clear()
        for peer in fresh:
            self._peer_queue.put(peer)
        if fresh and self.on_peers is not None:
            try:
                self.on_peers(fresh)
            except Exception:
                self.logger.debug("DHT peer callback failed", exc_info=True)

    def _store_peer(self, info_hash: bytes, peer: tuple[str, int]) -> None:
        if not 0 < peer[1] < 65536:
            return
        with self._lock:
            bucket = self._announced.setdefault(info_hash, [])
            if peer in bucket:
                return
            bucket.append(peer)
            del bucket[:-MAX_STORED_PEERS]

    def _stored_peers(self, info_hash: bytes) -> list[tuple[str, int]]:
        with self._lock:
            return list(self._announced.get(info_hash, []))[:50]

    def _make_token(self, host: str) -> bytes:
        return hashlib.sha1(self._secret + host.encode("utf-8", "replace")).digest()[:8]

    def _valid_token(self, host: str, token: bytes) -> bool:
        return hmac.compare_digest(self._make_token(host), token)
