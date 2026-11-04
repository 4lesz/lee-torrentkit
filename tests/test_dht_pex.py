import os
import socket
import struct
import tempfile
import threading
import time
import unittest

from torrentkit import bencode
from torrentkit.create import create_torrent
from torrentkit.dht import DHTNode
from torrentkit.download import Downloader
from torrentkit.metainfo import Torrent
from torrentkit.peers import (
    EXTENSION_ID,
    HANDSHAKE_LEN,
    LOCAL_PEX_ID,
    UT_PEX,
    build_handshake,
    decode_compact_peers,
    encode_compact_peers,
    make_peer_id,
)
from torrentkit.seeder import LocalSeeder

from .helpers import build_content, make_torrent

LOOPBACK = "127.0.0.1"


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class DHTTests(unittest.TestCase):
    def setUp(self):
        self.info_hash = os.urandom(20)
        self.nodes = []

    def tearDown(self):
        for node in self.nodes:
            node.stop()

    def _node(self, **kwargs):
        kwargs.setdefault("bootstrap", ())
        node = DHTNode(self.info_hash, **kwargs)
        self.nodes.append(node)
        return node

    def test_get_peers_and_announce_between_two_local_nodes(self):
        seed = self._node(announce_port=7001).start()
        seed.publish_peer(("10.1.2.3", 51413))

        client = self._node(
            announce_port=7002, bootstrap=[(LOOPBACK, seed.port)], query_interval=0.5
        ).start()
        peers = client.collect_peers(5.0)

        self.assertIn(("10.1.2.3", 51413), peers)
        self.assertGreaterEqual(client.node_count, 1)
        # The get_peers reply carries a token, so the client announces itself.
        self.assertTrue(
            wait_until(lambda: (LOOPBACK, 7002) in seed._stored_peers(self.info_hash)),
            seed._stored_peers(self.info_hash),
        )

    def test_unsolicited_responses_are_ignored(self):
        node = self._node().start()
        values = [socket.inet_aton("10.9.9.9") + struct.pack(">H", 6881)]
        reply = bencode.encode(
            {b"t": b"zz", b"y": b"r", b"r": {b"id": os.urandom(20), b"values": values}}
        )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(reply, (LOOPBACK, node.port))
        self.assertEqual(node.collect_peers(1.0), [])
        self.assertEqual(node.node_count, 0)

    def test_announce_with_bad_token_is_rejected(self):
        node = self._node().start()
        query = bencode.encode(
            {
                b"t": b"aa",
                b"y": b"q",
                b"q": b"announce_peer",
                b"a": {
                    b"id": os.urandom(20),
                    b"info_hash": self.info_hash,
                    b"port": 6881,
                    b"token": b"forged!!",
                },
            }
        )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3.0)
            sock.sendto(query, (LOOPBACK, node.port))
            reply = bencode.decode(sock.recv(65535), strict=False)
        self.assertEqual(reply[b"y"], b"e")
        self.assertEqual(node._stored_peers(self.info_hash), [])


class DHTDownloadTests(unittest.TestCase):
    def test_download_finds_seeder_through_dht_only(self):
        with tempfile.TemporaryDirectory() as root:
            content = os.path.join(root, "content")
            os.makedirs(content)
            blobs = build_content(content)
            torrent = make_torrent(content)
            seeder = LocalSeeder(torrent, root, host=LOOPBACK, port=0)
            seed_port = seeder.start()
            dht = DHTNode(torrent.info_hash, bootstrap=()).start()
            try:
                dht.publish_peer((LOOPBACK, seed_port))
                out = os.path.join(root, "out")
                downloader = Downloader(
                    torrent,
                    out,
                    port=0,
                    peer_id=make_peer_id(),
                    use_dht=True,
                    dht_bootstrap=[(LOOPBACK, dht.port)],
                    idle_timeout=15.0,
                )
                result = {}
                worker = threading.Thread(
                    target=lambda: result.setdefault("ok", downloader.run(poll=0.1))
                )
                worker.start()
                worker.join(30.0)
                self.assertFalse(worker.is_alive(), "download did not finish")
                self.assertTrue(result.get("ok"))
                self.assertIsNone(downloader._dht, "DHT node should be stopped")
                for relative, blob in blobs.items():
                    with open(os.path.join(out, "content", relative), "rb") as handle:
                        self.assertEqual(handle.read(), blob, relative)
            finally:
                dht.stop()
                seeder.stop()


class _FakePexPeer(threading.Thread):
    """Speaks just enough of the wire protocol to exchange ut_pex messages."""

    PEER_PEX_ID = 5

    def __init__(self, info_hash: bytes, advertise: list[tuple[str, int]]):
        super().__init__(daemon=True)
        self.info_hash = info_hash
        self.advertise = advertise
        self.server = socket.create_server((LOOPBACK, 0))
        self.server.settimeout(10.0)
        self.port = self.server.getsockname()[1]
        self.client_mapping: dict | None = None
        self.received: list[tuple[str, int]] = []
        self.done = threading.Event()
        self.error: BaseException | None = None

    def _read_exact(self, conn, size):
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    def _read_message(self, conn):
        while True:
            length = struct.unpack(">I", self._read_exact(conn, 4))[0]
            if length:
                body = self._read_exact(conn, length)
                return body[0], body[1:]

    def _send_extension(self, conn, ext_id, payload):
        body = bytes([EXTENSION_ID, ext_id]) + bencode.encode(payload)
        conn.sendall(struct.pack(">I", len(body)) + body)

    def run(self):
        try:
            conn, _ = self.server.accept()
            with conn:
                conn.settimeout(10.0)
                self._read_exact(conn, HANDSHAKE_LEN)
                conn.sendall(build_handshake(self.info_hash, make_peer_id(), extensions=True))
                while True:
                    message_id, payload = self._read_message(conn)
                    if message_id != EXTENSION_ID:
                        continue
                    if payload[0] == 0:
                        handshake = bencode.decode(payload[1:], strict=False)
                        self.client_mapping = handshake.get(b"m", {})
                        if UT_PEX not in self.client_mapping:
                            return
                        self._send_extension(conn, 0, {b"m": {UT_PEX: self.PEER_PEX_ID}})
                        added, _ = encode_compact_peers(self.advertise)
                        self._send_extension(
                            conn, self.client_mapping[UT_PEX], {b"added": added}
                        )
                    elif payload[0] == self.PEER_PEX_ID:
                        message = bencode.decode(payload[1:], strict=False)
                        self.received = decode_compact_peers(message.get(b"added", b""))
                        return
        except BaseException as exc:  # surfaced by the test
            self.error = exc
        finally:
            self.done.set()
            self.server.close()


class PexTests(unittest.TestCase):
    def test_compact_peer_round_trip(self):
        peers = [("10.0.0.1", 6881), ("::1", 51413), ("bad host", 1), ("10.0.0.2", 0)]
        ipv4, ipv6 = encode_compact_peers(peers)
        self.assertEqual(decode_compact_peers(ipv4), [("10.0.0.1", 6881)])
        self.assertEqual(decode_compact_peers(ipv6, version=6), [("::1", 51413)])

    def _run_against_fake(self, *, private: bool):
        with tempfile.TemporaryDirectory() as root:
            content = os.path.join(root, "content")
            os.makedirs(content)
            build_content(content)
            torrent = Torrent(create_torrent(content, piece_length=16384, private=private))
            fake = _FakePexPeer(torrent.info_hash, [("10.20.30.40", 6000)])
            fake.start()
            downloader = Downloader(
                torrent,
                os.path.join(root, "out"),
                port=0,
                extra_peers=[(LOOPBACK, fake.port), ("10.99.99.99", 9999)],
                use_dht=False,
                connect_timeout=2.0,
            )
            stop = threading.Event()
            worker = threading.Thread(
                target=downloader.run, kwargs={"stop_event": stop, "poll": 0.1}
            )
            worker.start()
            try:
                self.assertTrue(fake.done.wait(15.0), "fake peer never finished")
                self.assertIsNone(fake.error, fake.error)
                wait_until(lambda: ("10.20.30.40", 6000) in downloader._seen, 3.0)
                return fake, set(downloader._seen)
            finally:
                stop.set()
                worker.join(10.0)

    def test_pex_messages_are_exchanged_with_peer(self):
        fake, seen = self._run_against_fake(private=False)
        self.assertEqual(fake.client_mapping.get(UT_PEX), LOCAL_PEX_ID)
        self.assertIn(("10.20.30.40", 6000), seen)
        self.assertIn(("10.99.99.99", 9999), fake.received)
        self.assertNotIn((LOOPBACK, fake.port), fake.received)

    def test_private_torrent_does_not_use_pex(self):
        fake, seen = self._run_against_fake(private=True)
        self.assertNotIn(UT_PEX, fake.client_mapping)
        self.assertNotIn(("10.20.30.40", 6000), seen)


if __name__ == "__main__":
    unittest.main()
