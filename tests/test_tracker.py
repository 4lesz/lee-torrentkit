import unittest
from urllib.parse import unquote_to_bytes

from torrentkit.tracker import (
    announce,
    announce_all,
    announce_http,
    announce_udp,
    collect_peers,
)

from .helpers import HttpTracker, UdpTracker

INFO_HASH = bytes(range(20))
PEER_ID = b"-TK0100-abcdefghijkl"
LEFT = 123456


class TrackerTests(unittest.TestCase):
    def test_http_announce(self):
        peer = ("127.0.0.1", 51413)
        with HttpTracker([peer], info_hash=INFO_HASH, interval=7) as tracker:
            result = announce_http(
                tracker.url,
                info_hash=INFO_HASH,
                peer_id=PEER_ID,
                port=6881,
                left=LEFT,
                event="started",
            )
            seen = tracker.seen[0]
        self.assertTrue(result.ok)
        self.assertEqual(result.interval, 7)
        self.assertEqual(result.peers, [peer])
        self.assertEqual(unquote_to_bytes(seen["info_hash"]), INFO_HASH)
        self.assertEqual(seen["port"], "6881")
        self.assertEqual(seen["compact"], "1")
        self.assertEqual(seen["event"], "started")
        self.assertEqual(seen["left"], str(LEFT))

    def test_http_failure_reason_is_reported(self):
        with HttpTracker([("127.0.0.1", 1)], info_hash=b"\xff" * 20) as tracker:
            result = announce_http(
                tracker.url, info_hash=INFO_HASH, peer_id=PEER_ID, port=6881
            )
        self.assertFalse(result.ok)
        self.assertIn("info hash", result.failure or "")

    def test_udp_announce(self):
        peer = ("127.0.0.1", 60001)
        with UdpTracker([peer]) as tracker:
            result = announce_udp(
                tracker.url,
                info_hash=INFO_HASH,
                peer_id=PEER_ID,
                port=6881,
                left=LEFT,
                event="started",
            )
            announced = tracker.announced[0]
        self.assertTrue(result.ok, result.failure)
        self.assertEqual(result.interval, 1)
        self.assertEqual(result.peers, [peer])
        self.assertEqual(result.seeders, 1)
        self.assertEqual(announced, INFO_HASH)

    def test_dispatch_http_and_unsupported_scheme(self):
        with HttpTracker([("127.0.0.1", 4711)]) as tracker:
            result = announce(
                tracker.url, info_hash=INFO_HASH, peer_id=PEER_ID, port=6881
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.peers, [("127.0.0.1", 4711)])

        unsupported = announce("ftp://tracker/announce", info_hash=INFO_HASH, peer_id=PEER_ID, port=1)
        self.assertFalse(unsupported.ok)
        self.assertIn("unsupported", unsupported.failure or "")

    def test_announce_all_deduplicates_and_collects_peers(self):
        with HttpTracker([("127.0.0.1", 1234)]) as tracker:
            results = announce_all(
                [tracker.url, tracker.url],
                info_hash=INFO_HASH,
                peer_id=PEER_ID,
                port=6881,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(collect_peers(results), [("127.0.0.1", 1234)])

    def test_invalid_peer_id_is_rejected_before_networking(self):
        with self.assertRaises(ValueError):
            announce_http("http://127.0.0.1:9/announce", info_hash=INFO_HASH, peer_id=b"short", port=1)


if __name__ == "__main__":
    unittest.main()
