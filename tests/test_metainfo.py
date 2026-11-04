import base64
import hashlib
import os
import tempfile
import unittest

from torrentkit import bencode
from torrentkit.create import create_torrent
from torrentkit.metainfo import Magnet, MetainfoError, Torrent

from .helpers import build_content


class MetainfoTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = self._temp.name

    def tearDown(self):
        self._temp.cleanup()

    def test_single_file_torrent(self):
        path = os.path.join(self.root, "single.bin")
        with open(path, "wb") as handle:
            handle.write(os.urandom(5000))
        torrent = Torrent(create_torrent(path, piece_length=16384))
        self.assertTrue(torrent.is_single_file)
        self.assertEqual(torrent.name, "single.bin")
        self.assertEqual(torrent.total_length, 5000)
        self.assertEqual(torrent.piece_count, 1)
        self.assertEqual(torrent.piece_size(0), 5000)
        self.assertEqual(torrent.files[0].path, ())
        self.assertFalse(torrent.is_v2)

    def test_multi_file_layout_and_piece_sizes(self):
        content = os.path.join(self.root, "content")
        os.makedirs(content)
        build_content(content)
        torrent = Torrent(create_torrent(content, piece_length=16384))

        self.assertFalse(torrent.is_single_file)
        self.assertEqual(torrent.total_length, 160000)
        self.assertEqual(torrent.piece_count, 10)
        self.assertEqual([torrent.piece_size(i) for i in range(9)], [16384] * 9)
        self.assertEqual(torrent.piece_size(9), 12544)

        offsets = {entry.path: entry.offset for entry in torrent.files}
        self.assertEqual(offsets[("a.bin",)], 0)
        self.assertEqual(offsets[("empty.txt",)], 70000)
        self.assertEqual(offsets[("sub", "b.bin")], 70000)
        self.assertIn("content/sub/b.bin", torrent.files_display())

    def test_info_hash_uses_the_raw_info_bytes(self):
        piece = os.urandom(100)
        pieces = hashlib.sha1(piece).digest()
        canonical = (
            b"d6:lengthi100e4:name5:a.bin12:piece lengthi16384e6:pieces20:" + pieces + b"e"
        )
        # Keys deliberately out of order: other clients still hash these bytes.
        unsorted = (
            b"d4:name5:a.bin6:lengthi100e6:pieces20:" + pieces + b"12:piece lengthi16384ee"
        )
        for info in (canonical, unsorted):
            data = b"d8:announce14:http://t/annou4:info" + info + b"e"
            torrent = Torrent.from_bytes(data)
            self.assertEqual(torrent.info_hash, hashlib.sha1(info).digest())
        self.assertEqual(bencode.encode(bencode.decode(canonical)), canonical)

    def test_magnet_roundtrip(self):
        content = os.path.join(self.root, "content")
        os.makedirs(content)
        build_content(content)
        metainfo = create_torrent(
            content,
            piece_length=16384,
            trackers=["http://tracker.example/announce", "udp://tracker.example:6969"],
        )
        metainfo[b"url-list"] = [b"http://seed.example/files/"]
        torrent = Torrent(metainfo)

        magnet = Magnet.parse(torrent.magnet_uri())
        self.assertEqual(magnet.info_hash, torrent.info_hash)
        self.assertEqual(magnet.dn, "content")
        self.assertEqual(magnet.xl, torrent.total_length)
        self.assertEqual(magnet.trackers, torrent.trackers)
        self.assertEqual(magnet.web_seeds, torrent.web_seeds)
        self.assertTrue(magnet.has_v1_hash)

    def test_magnet_supports_base32_info_hashes(self):
        info_hash = bytes(range(20))
        encoded = base64.b32encode(info_hash).decode("ascii").lower()
        magnet = Magnet.parse(
            f"magnet:?xt=urn:btih:{encoded}&dn=Test%20Name&tr=udp%3A%2F%2Ftracker%3A80"
        )
        self.assertEqual(magnet.info_hash, info_hash)
        self.assertEqual(magnet.dn, "Test Name")
        self.assertEqual(magnet.trackers, ["udp://tracker:80"])

    def test_magnet_requires_a_magnet_uri(self):
        with self.assertRaises(MetainfoError):
            Magnet.parse("http://example.com/x.torrent")

    def test_v2_only_file_tree(self):
        info = {
            b"name": b"dir",
            b"piece length": 16384,
            b"meta version": 2,
            b"file tree": {
                b"b.bin": {b"": {b"length": 3}},
                b"nested": {b"a.bin": {b"": {b"length": 2}}},
            },
        }
        torrent = Torrent({b"info": info})
        self.assertTrue(torrent.is_v2)
        self.assertFalse(torrent.is_hybrid)
        self.assertFalse(torrent.has_v1_hashes)
        self.assertEqual(torrent.piece_count, 0)
        self.assertEqual(torrent.total_length, 5)
        self.assertEqual(torrent.files[0].path, ("b.bin",))
        self.assertEqual(torrent.files[1].path, ("nested", "a.bin"))
        self.assertEqual(torrent.files[1].offset, 3)

        magnet = Magnet.parse(torrent.magnet_uri())
        self.assertEqual(magnet.info_hash_v2, torrent.info_hash_v2)
        self.assertFalse(magnet.has_v1_hash)

    def test_rejects_unsafe_paths(self):
        info = {
            b"name": b"evil",
            b"piece length": 16384,
            b"pieces": b"\x00" * 20,
            b"files": [{b"length": 1, b"path": [b"..", b"escape"]}],
        }
        with self.assertRaises(MetainfoError):
            Torrent({b"info": info})

    def test_rejects_piece_count_mismatch(self):
        info = {
            b"name": b"x",
            b"length": 100,
            b"piece length": 16384,
            b"pieces": b"\x00" * 40,
        }
        with self.assertRaises(MetainfoError):
            Torrent({b"info": info})

    def test_rejects_missing_fields(self):
        with self.assertRaises(MetainfoError):
            Torrent({b"info": {b"name": b"x"}})
        with self.assertRaises(MetainfoError):
            Torrent({b"info": {b"name": b"x", b"piece length": 16384}})


if __name__ == "__main__":
    unittest.main()
