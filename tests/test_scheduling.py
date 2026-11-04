import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from torrentkit import download as download_module
from torrentkit.download import Downloader
from torrentkit.peers import (
    BITFIELD,
    HANDSHAKE_LEN,
    build_handshake,
    make_peer_id,
    send_message,
)
from torrentkit.seeder import LocalSeeder

from .helpers import build_content, make_torrent


class _ChokingPeer(threading.Thread):
    """Has every piece but never unchokes us."""

    def __init__(self, torrent):
        super().__init__(daemon=True)
        self.torrent = torrent
        self.server = socket.create_server(("127.0.0.1", 0))
        self.server.settimeout(0.5)
        self.port = self.server.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            try:
                conn.settimeout(10.0)
                conn.recv(HANDSHAKE_LEN)
                conn.sendall(build_handshake(self.torrent.info_hash, make_peer_id()))
                count = self.torrent.piece_count
                send_message(conn, BITFIELD, b"\xff" * ((count + 7) // 8))
                while not self._stop.is_set() and conn.recv(65536):
                    pass
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        self.server.close()


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = self._temp.name
        content = os.path.join(self.root, "content")
        os.makedirs(content)
        self.blobs = build_content(content)
        self.torrent = make_torrent(content)

    def tearDown(self):
        self._temp.cleanup()

    def _run(self, downloader, timeout):
        stop = threading.Event()
        timer = threading.Timer(timeout, stop.set)
        timer.start()
        try:
            return downloader.run(stop_event=stop, poll=0.05)
        finally:
            timer.cancel()

    def test_peer_that_keeps_us_choked_gives_up_its_slot(self):
        choker = _ChokingPeer(self.torrent)
        choker.start()
        seeder = LocalSeeder(self.torrent, self.root, host="127.0.0.1", port=0)
        seed_port = seeder.start()
        try:
            downloader = Downloader(
                self.torrent,
                os.path.join(self.root, "out"),
                port=0,
                # The choker is first in line and there is only one slot.
                extra_peers=[("127.0.0.1", choker.port), ("127.0.0.1", seed_port)],
                max_peers=1,
                use_dht=False,
            )
            with mock.patch.object(download_module, "CHOKED_EVICT_AFTER", 0.5), \
                    mock.patch.object(download_module, "MAX_HALF_OPEN", 1):
                ok = self._run(downloader, 20.0)
        finally:
            seeder.stop()
            choker.stop()
        self.assertTrue(ok, "the choking peer kept the only slot")
        self.assertGreaterEqual(choker.connections, 1)

    def test_drop_only_disconnects_one_peer(self):
        seeder = LocalSeeder(self.torrent, self.root, host="127.0.0.1", port=0)
        port = seeder.start()
        try:
            downloader = Downloader(
                self.torrent,
                os.path.join(self.root, "out"),
                port=0,
                extra_peers=[("127.0.0.1", port)],
                use_dht=False,
            )
            downloader.peer_retry_delay = 0.1
            dropped = []

            def drop_first_session():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not dropped:
                    for session in list(downloader._active.values()):
                        if session.connected:
                            session.drop()
                            dropped.append(session)
                            return
                    time.sleep(0.01)

            threading.Thread(target=drop_first_session, daemon=True).start()
            ok = self._run(downloader, 20.0)
        finally:
            seeder.stop()
        self.assertTrue(dropped, "no session was dropped")
        self.assertTrue(ok, "dropping one peer stopped the whole download")

    def test_unreachable_addresses_are_given_up(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = ("127.0.0.1", probe.getsockname()[1])
        # Nothing listens on ``dead`` now, so every connect is refused.
        downloader = Downloader(
            self.torrent,
            os.path.join(self.root, "out"),
            port=0,
            extra_peers=[dead],
            use_dht=False,
            connect_timeout=1.0,
        )
        downloader.peer_retry_delay = 0.05
        # Windows takes about a second to refuse a loopback connect.
        self.assertFalse(self._run(downloader, 8.0))
        self.assertEqual(downloader._failures.get(dead), download_module.MAX_DEAD_ATTEMPTS)
        self.assertNotIn(dead, downloader._retry)


if __name__ == "__main__":
    unittest.main()
