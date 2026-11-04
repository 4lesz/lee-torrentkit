import contextlib
import io
import json
import os
import tempfile
import threading
import unittest

from torrentkit.cli import main
from torrentkit.create import write_torrent
from torrentkit.download import DownloadError, Downloader, resolve_source
from torrentkit.metainfo import Magnet, Torrent
from torrentkit.peers import fetch_metadata, make_peer_id
from torrentkit.seeder import LocalSeeder, SeederError

from .helpers import HttpTracker, build_content, make_torrent


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.root = cls._temp.name
        cls.content = os.path.join(cls.root, "content")
        os.makedirs(cls.content)
        cls.blobs = build_content(cls.content)
        cls.torrent = make_torrent(cls.content)
        cls.seeder = LocalSeeder(cls.torrent, cls.root, host="127.0.0.1", port=0)
        cls.seed_port = cls.seeder.start()
        cls.peer_id = make_peer_id()

    @classmethod
    def tearDownClass(cls):
        cls.seeder.stop()
        cls._temp.cleanup()

    def _peer(self):
        return [("127.0.0.1", self.seed_port)]

    def _assert_payload(self, out_dir: str) -> None:
        base = os.path.join(out_dir, "content")
        for relative, blob in self.blobs.items():
            with open(os.path.join(base, relative), "rb") as handle:
                self.assertEqual(handle.read(), blob, relative)

    def test_seeder_refuses_a_port_that_is_already_listening(self):
        second = LocalSeeder(self.torrent, self.root, host="127.0.0.1", port=self.seed_port)
        with self.assertRaises(SeederError) as context:
            second.start()
        self.assertIn("already in use", str(context.exception))

    def test_direct_peer_download(self):
        out = os.path.join(self.root, "direct")
        progress = []
        downloader = Downloader(
            self.torrent,
            out,
            port=0,
            peer_id=self.peer_id,
            extra_peers=self._peer(),
            idle_timeout=15.0,
            use_dht=False,
        )
        self.assertTrue(downloader.run(progress.append, poll=0.1))
        self.assertTrue(progress)
        self.assertEqual(progress[-1].fraction, 1.0)
        self._assert_payload(out)

    def test_pieces_larger_than_the_request_pipeline(self):
        # 256 KiB pieces are 16 blocks; the session only keeps 8 in flight, so
        # it must keep requesting as blocks arrive instead of stalling at 8/16.
        content = os.path.join(self.root, "big")
        os.makedirs(content)
        blob = os.urandom(600_000)
        with open(os.path.join(content, "big.bin"), "wb") as handle:
            handle.write(blob)
        torrent = make_torrent(content, piece_length=256 * 1024)
        seeder = LocalSeeder(torrent, self.root, host="127.0.0.1", port=0)
        port = seeder.start()
        try:
            out = os.path.join(self.root, "big-out")
            downloader = Downloader(
                torrent,
                out,
                port=0,
                peer_id=self.peer_id,
                extra_peers=[("127.0.0.1", port)],
                idle_timeout=15.0,
                use_dht=False,
            )
            stop = threading.Event()
            timer = threading.Timer(20.0, stop.set)
            timer.start()
            try:
                self.assertTrue(downloader.run(stop_event=stop, poll=0.1), "download stalled")
            finally:
                timer.cancel()
        finally:
            seeder.stop()
        with open(os.path.join(out, "big", "big.bin"), "rb") as handle:
            self.assertEqual(handle.read(), blob)

    def test_resume_after_losing_part_of_the_payload(self):
        out = os.path.join(self.root, "resume")
        first = Downloader(
            self.torrent,
            out,
            port=0,
            peer_id=self.peer_id,
            extra_peers=self._peer(),
            idle_timeout=15.0,
            use_dht=False,
        )
        self.assertTrue(first.run(poll=0.1))

        # Simulate a damaged file: the affected pieces must be re-fetched.
        with open(os.path.join(out, "content", "sub", "b.bin"), "wb"):
            pass

        second = Downloader(
            self.torrent,
            out,
            port=0,
            peer_id=self.peer_id,
            extra_peers=self._peer(),
            idle_timeout=15.0,
            use_dht=False,
        )
        self.assertTrue(second.run(poll=0.1))
        self._assert_payload(out)

    def test_fetch_metadata_from_peer(self):
        info = fetch_metadata(
            self.torrent.info_hash,
            self._peer(),
            self.peer_id,
            timeout=10.0,
        )
        self.assertEqual(info, self.torrent.info)

    def test_magnet_resolution_and_download(self):
        with HttpTracker(self._peer(), info_hash=self.torrent.info_hash) as tracker:
            torrent = Torrent.from_info(self.torrent.info, trackers=[tracker.url])
            magnet = torrent.magnet_uri()
            resolved, magnet_obj = resolve_source(
                magnet, peer_id=self.peer_id, port=6881, timeout=10.0, use_dht=False
            )
            self.assertIsNotNone(magnet_obj)
            self.assertEqual(resolved.info_hash, torrent.info_hash)
            self.assertEqual(resolved.name, "content")

            out = os.path.join(self.root, "magnet")
            downloader = Downloader(
                resolved,
                out,
                port=6881,
                peer_id=self.peer_id,
                idle_timeout=15.0,
                use_dht=False,
            )
            self.assertTrue(downloader.run(poll=0.1))
            self._assert_payload(out)

    def test_magnet_without_peers_fails_fast(self):
        magnet = Magnet.parse(
            f"magnet:?xt=urn:btih:{self.torrent.info_hash.hex()}&dn=content"
        )
        with self.assertRaises(DownloadError):
            resolve_source(
                magnet.uri, extra_peers=(), peer_id=self.peer_id, port=6881, use_dht=False
            )

    def test_cli_info_and_magnet(self):
        torrent_path = os.path.join(self.root, "cli.torrent")
        write_torrent(self.torrent.metainfo, torrent_path)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["info", torrent_path, "--json"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["info_hash"], self.torrent.info_hash.hex())
        self.assertEqual(payload["total_length"], self.torrent.total_length)
        self.assertEqual(len(payload["files"]), 3)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["magnet", torrent_path]), 0)
        self.assertTrue(output.getvalue().strip().startswith("magnet:?xt=urn:btih:"))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["info", torrent_path]), 0)
        self.assertIn("content/sub/b.bin", output.getvalue())

    def test_cli_create(self):
        output_path = os.path.join(self.root, "created.torrent")
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "create",
                    self.content,
                    "-o",
                    output_path,
                    "--piece-length",
                    "16KiB",
                    "--tracker",
                    "udp://tracker.example:6969",
                ]
            )
        self.assertEqual(code, 0)
        created = Torrent.from_file(output_path)
        self.assertEqual(created.info_hash, self.torrent.info_hash)
        self.assertEqual(created.trackers, ["udp://tracker.example:6969"])

    def test_cli_download(self):
        torrent_path = os.path.join(self.root, "cli-download.torrent")
        write_torrent(self.torrent.metainfo, torrent_path)
        out = os.path.join(self.root, "cli-download")
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "download",
                    torrent_path,
                    "-d",
                    out,
                    "--port",
                    "0",
                    "--peer",
                    f"127.0.0.1:{self.seed_port}",
                    "--no-dht",
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        self._assert_payload(out)


if __name__ == "__main__":
    unittest.main()
