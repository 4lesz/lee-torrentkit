import hashlib
import os
import tempfile
import threading
import unittest

from torrentkit.create import create_torrent
from torrentkit.metainfo import Torrent
from torrentkit.storage import FileStorage, StorageError

from .helpers import build_content


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = self._temp.name
        self.content = os.path.join(self.root, "content")
        os.makedirs(self.content)
        self.blobs = build_content(self.content)
        self.torrent = Torrent(create_torrent(self.content, piece_length=16384))
        self.source = FileStorage(self.torrent, self.root)

    def tearDown(self):
        self.source.close()
        self._temp.cleanup()

    def test_pieces_span_file_boundaries(self):
        destination = FileStorage(self.torrent, os.path.join(self.root, "out"))
        destination.prepare()
        try:
            self.assertFalse(any(destination.verify_all()))
            for index in range(self.torrent.piece_count):
                destination.write_piece(index, self.source.read_piece(index))
            self.assertTrue(all(destination.verify_all()))
            for entry in self.torrent.files:
                with open(destination.target_path(entry), "rb") as handle:
                    data = handle.read()
                self.assertEqual(data, self.blobs["/".join(entry.path)])
        finally:
            destination.close()

    def test_resume_verification_reuses_written_pieces(self):
        destination_root = os.path.join(self.root, "out")
        first = FileStorage(self.torrent, destination_root)
        first.prepare()
        for index in range(self.torrent.piece_count):
            first.write_piece(index, self.source.read_piece(index))
        first.close()

        second = FileStorage(self.torrent, destination_root)
        second.prepare()
        try:
            flags = second.verify_all()
            self.assertTrue(all(flags))
        finally:
            second.close()

    def test_missing_file_reads_as_zeroes(self):
        destination_root = os.path.join(self.root, "out")
        storage = FileStorage(self.torrent, destination_root)
        storage.prepare()
        try:
            data = storage.read_range(0, 32)
            self.assertEqual(data, b"\x00" * 32)
        finally:
            storage.close()

    def test_verify_only_hashes_pieces_with_written_data(self):
        destination_root = os.path.join(self.root, "out")
        destination = FileStorage(self.torrent, destination_root)
        destination.prepare()
        destination.write_piece(0, self.source.read_piece(0))
        try:
            flags = destination.verify_all()
            self.assertTrue(flags[0])
            self.assertFalse(any(flags[1:]))
        finally:
            destination.close()

    def test_prepare_does_not_preallocate_full_size(self):
        destination_root = os.path.join(self.root, "out")
        storage = FileStorage(self.torrent, destination_root)
        storage.prepare()
        try:
            for entry in self.torrent.files:
                size = storage.target_path(entry).stat().st_size
                self.assertLessEqual(size, entry.length)
                if entry.length > 0:
                    self.assertEqual(size, 0)
        finally:
            storage.close()

    def test_prepare_reports_per_file_progress_and_can_stop(self):
        destination_root = os.path.join(self.root, "out")
        storage = FileStorage(self.torrent, destination_root)
        seen = []
        self.assertTrue(storage.prepare(lambda done, total: seen.append((done, total))))
        self.assertEqual(seen[-1], (len(self.torrent.files), len(self.torrent.files)))
        storage.close()

        blocked = FileStorage(self.torrent, os.path.join(self.root, "blocked"))
        stop = threading.Event()
        stop.set()
        self.assertFalse(blocked.prepare(stop_event=stop))
        blocked.close()

    def test_verify_all_respects_stop_event(self):
        destination_root = os.path.join(self.root, "out")
        storage = FileStorage(self.torrent, destination_root)
        stop = threading.Event()
        stop.set()
        flags = storage.verify_all(stop_event=stop)
        self.assertEqual(len(flags), self.torrent.piece_count)
        self.assertFalse(any(flags))

    def test_partial_piece_write_is_rejected(self):
        storage = FileStorage(self.torrent, os.path.join(self.root, "out"))
        storage.prepare()
        try:
            with self.assertRaises(StorageError):
                storage.write_piece(0, b"short")
        finally:
            storage.close()

    def test_block_reads_and_hashes(self):
        block = self.source.read_block(0, 0, 16384)
        self.assertEqual(len(block), 16384)
        self.assertEqual(block, self.source.read_piece(0)[:16384])
        self.assertEqual(
            hashlib.sha1(self.source.read_piece(0)).digest(), self.torrent.piece_hashes[0]
        )


if __name__ == "__main__":
    unittest.main()
