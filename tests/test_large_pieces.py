import os
import tempfile
import threading
import unittest
from unittest import mock

from torrentkit import seeder as seeder_module
from torrentkit.download import Downloader
from torrentkit.peers import make_peer_id
from torrentkit.pieces import TorrentState
from torrentkit.seeder import LocalSeeder
from torrentkit.storage import FileStorage

from .helpers import make_torrent


def _write_blob(directory: str, size: int) -> bytes:
    os.makedirs(directory)
    blob = os.urandom(size)
    with open(os.path.join(directory, "data.bin"), "wb") as handle:
        handle.write(blob)
    return blob


class PartialPieceStateTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        content = os.path.join(self._temp.name, "content")
        _write_blob(content, 3 * 65536)
        self.torrent = make_torrent(content, piece_length=65536)
        self.storage = FileStorage(self.torrent, os.path.join(self._temp.name, "out"))
        self.storage.prepare()
        self.state = TorrentState(self.torrent, self.storage)

    def tearDown(self):
        self.storage.close()
        self._temp.cleanup()

    def test_released_blocks_are_handed_to_the_next_owner(self):
        index = self.state.acquire(1)
        data = bytearray(b"x" * 65536)
        self.state.release(index, 1, data, {0, 16384})
        # Another peer gets the half-finished piece first, with its blocks.
        self.assertEqual(self.state.acquire(2), index)
        saved = self.state.take_partial(index, 2)
        self.assertIsNotNone(saved)
        self.assertIs(saved[0], data)
        self.assertEqual(saved[1], {0, 16384})
        self.assertIsNone(self.state.take_partial(index, 2))

    def test_release_without_blocks_keeps_nothing(self):
        index = self.state.acquire(1)
        self.state.release(index, 1)
        self.assertEqual(self.state.snapshot()["partial_pieces"], 0)

    def test_partial_stash_is_bounded(self):
        with mock.patch("torrentkit.pieces.MAX_PARTIAL_BYTES", 65536 * 2):
            for owner in range(3):
                index = self.state.acquire(owner)
                self.state.release(index, owner, bytearray(65536), {0} if owner else {0, 1, 2})
                # Keep it out of the partial-first path so the next acquire is new.
                self.state._inflight[index] = 99
            self.assertEqual(self.state.snapshot()["partial_pieces"], 2)

    def test_received_bytes_count_unfinished_pieces(self):
        self.state.add_received(16384)
        self.assertEqual(self.state.received, 16384)
        self.assertEqual(self.state.downloaded, 0)


class KeepAliveTests(unittest.TestCase):
    def test_keep_alive_mid_transfer_is_not_a_choke(self):
        served = {}
        original = seeder_module._SeederConnection._serve_request

        def chatty(connection, payload):
            original(connection, payload)
            served[id(connection)] = served.get(id(connection), 0) + 1
            if served[id(connection)] == 3:
                # Arrives after the unchoke; the old code read it as CHOKE (id 0).
                connection.conn.sendall(b"\x00\x00\x00\x00")

        with tempfile.TemporaryDirectory() as root:
            content = os.path.join(root, "content")
            blob = _write_blob(content, 512 * 1024)
            torrent = make_torrent(content, piece_length=256 * 1024)
            seeder = LocalSeeder(torrent, root, host="127.0.0.1", port=0)
            port = seeder.start()
            try:
                with mock.patch.object(seeder_module._SeederConnection, "_serve_request", chatty):
                    downloader = Downloader(
                        torrent,
                        os.path.join(root, "out"),
                        port=0,
                        peer_id=make_peer_id(),
                        extra_peers=[("127.0.0.1", port)],
                        use_dht=False,
                    )
                    stop = threading.Event()
                    timer = threading.Timer(15.0, stop.set)
                    timer.start()
                    try:
                        ok = downloader.run(stop_event=stop, poll=0.05)
                    finally:
                        timer.cancel()
            finally:
                seeder.stop()
            self.assertTrue(ok, "download stalled after a keep-alive")
            self.assertEqual(len(served), 1, "the connection should never have been dropped")
            with open(os.path.join(root, "out", "content", "data.bin"), "rb") as handle:
                self.assertEqual(handle.read(), blob)


class FlakySeederTests(unittest.TestCase):
    BLOCKS_PER_CONNECTION = 20  # far fewer than the 64 blocks of a 1 MiB piece

    def test_large_pieces_finish_even_if_every_peer_drops_mid_piece(self):
        served = {}
        original = seeder_module._SeederConnection._serve_request

        def flaky(connection, payload):
            count = served.get(id(connection), 0)
            if count >= self.BLOCKS_PER_CONNECTION:
                raise ConnectionError("simulated drop")
            served[id(connection)] = count + 1
            original(connection, payload)

        with tempfile.TemporaryDirectory() as root:
            content = os.path.join(root, "content")
            blob = _write_blob(content, 2 * 1024 * 1024)
            torrent = make_torrent(content, piece_length=1024 * 1024)
            seeder = LocalSeeder(torrent, root, host="127.0.0.1", port=0)
            port = seeder.start()
            try:
                with mock.patch.object(seeder_module._SeederConnection, "_serve_request", flaky):
                    downloader = Downloader(
                        torrent,
                        os.path.join(root, "out"),
                        port=0,
                        peer_id=make_peer_id(),
                        extra_peers=[("127.0.0.1", port)],
                        use_dht=False,
                        idle_timeout=5.0,
                    )
                    downloader.peer_retry_delay = 0.2
                    progress = []
                    stop = threading.Event()
                    timer = threading.Timer(30.0, stop.set)
                    timer.start()
                    try:
                        ok = downloader.run(progress.append, stop_event=stop, poll=0.05)
                    finally:
                        timer.cancel()
            finally:
                seeder.stop()
            self.assertTrue(ok, "download never completed a piece")
            # Speed was reported before the first piece was verified.
            self.assertTrue(any(p.speed > 0 and p.pieces_done == 0 for p in progress))
            self.assertGreater(len(served), 2)
            with open(os.path.join(root, "out", "content", "data.bin"), "rb") as handle:
                self.assertEqual(handle.read(), blob)


if __name__ == "__main__":
    unittest.main()
