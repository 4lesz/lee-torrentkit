"""A small local seeder, useful for LAN sharing and end-to-end tests."""

from __future__ import annotations

import socket
import struct
import threading
import time

from . import bencode
from .metainfo import Torrent
from .peers import (
    BITFIELD,
    EXTENSION_ID,
    HANDSHAKE_LEN,
    INTERESTED,
    PIECE,
    PSTR,
    REQUEST,
    UNCHOKE,
    UT_METADATA,
    BufferedSocket,
    ProtocolError,
    build_handshake,
    make_peer_id,
    send_message,
)
from .sockets import configure_reuse, is_address_in_use
from .storage import FileStorage, StorageError

MAX_SERVED_REQUEST = 1024 * 1024


class SeederError(RuntimeError):
    pass


class LocalSeeder:
    """Serves the pieces of a torrent that are already present on disk."""

    def __init__(
        self,
        torrent: Torrent,
        root,
        *,
        host: str = "0.0.0.0",
        port: int = 6881,
        peer_id: bytes | None = None,
        verify: bool = True,
        logger=None,
    ):
        if not torrent.has_v1_hashes:
            raise SeederError("only v1/hybrid torrents can be seeded by this client")
        self.torrent = torrent
        self.host = host
        self.port = port
        self.peer_id = peer_id or make_peer_id()
        self.logger = logger

        self.storage = FileStorage(torrent, root)
        self.storage.prepare()
        if verify:
            flags = self.storage.verify_all()
            self.have = {index for index, ok in enumerate(flags) if ok}
        else:
            self.have = set(range(torrent.piece_count))
        if not self.have:
            self.storage.close()
            raise SeederError("no verified pieces available to seed")

        self._stop = threading.Event()
        self._listen: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()
        self.bound_port: int | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> int:
        if self._listen is not None:
            raise SeederError("seeder is already running")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        configure_reuse(sock)
        try:
            sock.bind((self.host, self.port))
            sock.listen(32)
            sock.settimeout(1.0)
        except OSError as exc:
            sock.close()
            # Release file handles so the data is not left locked (Windows);
            # they reopen lazily if start() is retried.
            self.storage.close()
            if is_address_in_use(exc):
                raise SeederError(
                    f"port {self.port} on {self.host} is already in use; pick another --port"
                ) from exc
            raise
        self._listen = sock
        self.bound_port = sock.getsockname()[1]
        self._thread = threading.Thread(
            target=self._accept_loop, name="torrentkit-seeder", daemon=True
        )
        self._thread.start()
        return self.bound_port

    def stop(self) -> None:
        self._stop.set()
        if self._listen is not None:
            try:
                self._listen.close()
            except OSError:
                pass
            self._listen = None
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.storage.close()

    # -- internals --------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            assert self._listen is not None
            try:
                conn, address = self._listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(10.0)
            with self._lock:
                self._clients.add(conn)
            handler = _SeederConnection(self, conn, address)
            handler.start()

    def _forget(self, conn: socket.socket) -> None:
        with self._lock:
            self._clients.discard(conn)


class _SeederConnection(threading.Thread):
    def __init__(self, seeder: LocalSeeder, conn: socket.socket, address):
        super().__init__(daemon=True, name=f"seed-{address[0]}:{address[1]}")
        self.seeder = seeder
        self.conn = conn
        self.address = address
        self.peer_meta_id: int | None = None
        self.metadata = bencode.encode(seeder.torrent.info)

    def run(self) -> None:
        try:
            self._serve()
        except (OSError, EOFError, ConnectionError, ProtocolError, bencode.BencodeError) as exc:
            if self.seeder.logger:
                self.seeder.logger.debug(
                    "seeding connection %s:%s closed: %s", self.address[0], self.address[1], exc
                )
        finally:
            try:
                self.conn.close()
            except OSError:
                pass
            self.seeder._forget(self.conn)

    def _serve(self) -> None:
        seeder = self.seeder
        torrent = seeder.torrent
        conn = self.conn
        buffered = BufferedSocket(conn)
        handshake = buffered.read_exact(HANDSHAKE_LEN)
        if handshake[0] != len(PSTR) or handshake[1:20] != PSTR:
            return
        reserved = handshake[20:28]
        if handshake[28:48] != torrent.info_hash:
            return
        client_extensions = bool(reserved[5] & 0x10)

        conn.sendall(build_handshake(torrent.info_hash, seeder.peer_id, extensions=True))
        self._send_bitfield()
        send_message(conn, UNCHOKE)
        if client_extensions:
            self._send_extended_handshake()

        conn.settimeout(1.0)
        last_activity = time.monotonic()
        while not seeder._stop.is_set():
            try:
                message_id, payload = buffered.read_message()
            except socket.timeout:
                if time.monotonic() - last_activity > 300:
                    return
                continue
            last_activity = time.monotonic()
            if message_id == REQUEST:
                self._serve_request(payload)
            elif message_id == EXTENSION_ID:
                self._handle_extension(payload)
            elif message_id == INTERESTED:
                send_message(conn, UNCHOKE)
            # everything else (choke, have, keepalive, ...) needs no answer

    def _send_bitfield(self) -> None:
        count = self.seeder.torrent.piece_count
        if count == 0:
            return
        payload = bytearray((count + 7) // 8)
        for index in self.seeder.have:
            if 0 <= index < count:
                payload[index // 8] |= 0x80 >> (index % 8)
        send_message(self.conn, BITFIELD, bytes(payload))

    def _send_extended_handshake(self) -> None:
        body = bencode.encode(
            {
                b"m": {UT_METADATA: 1},
                b"metadata_size": len(self.metadata),
                b"v": b"torrentkit/0.1.0",
            }
        )
        send_message(self.conn, EXTENSION_ID, b"\x00" + body)

    def _serve_request(self, payload: bytes) -> None:
        if len(payload) != 12:
            return
        index, begin, length = struct.unpack(">III", payload)
        if index not in self.seeder.have:
            return
        if length == 0 or length > MAX_SERVED_REQUEST:
            return
        try:
            block = self.seeder.storage.read_block(index, begin, length)
        except StorageError:
            return
        send_message(self.conn, PIECE, struct.pack(">II", index, begin) + block)

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
            mapping = handshake.get(b"m")
            if isinstance(mapping, dict) and UT_METADATA in mapping:
                self.peer_meta_id = int(mapping[UT_METADATA])
            return
        if extension_id != 1 or self.peer_meta_id is None:
            return
        try:
            header, offset = bencode.decode_prefix(body)
        except bencode.BencodeError:
            return
        if not isinstance(header, dict) or header.get(b"msg_type") != 0:
            return
        piece = header.get(b"piece")
        total = len(self.metadata)
        count = (total + 16383) // 16384
        if not isinstance(piece, int) or piece < 0 or piece >= count:
            return
        chunk = self.metadata[piece * 16384 : (piece + 1) * 16384]
        response = bencode.encode({b"msg_type": 1, b"piece": piece, b"total_size": total}) + chunk
        send_message(self.conn, EXTENSION_ID, bytes([self.peer_meta_id]) + response)


def seed_forever(seeder: LocalSeeder) -> None:
    """Block until interrupted, then stop the seeder."""
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        seeder.stop()
