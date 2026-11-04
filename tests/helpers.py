import os
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote_to_bytes, urlsplit

from torrentkit import bencode
from torrentkit.create import create_torrent
from torrentkit.metainfo import Torrent


def build_content(root: str) -> dict[str, bytes]:
    """Write a small multi-file tree and return the blobs by relative path."""
    blobs = {
        "a.bin": os.urandom(70000),
        "empty.txt": b"",
        "sub/b.bin": os.urandom(90000),
    }
    for relative, blob in blobs.items():
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(blob)
    return blobs


def make_torrent(directory: str, *, trackers=(), piece_length: int = 16384) -> Torrent:
    return Torrent(create_torrent(directory, piece_length=piece_length, trackers=trackers))


def compact_peers(peers) -> bytes:
    return b"".join(
        socket.inet_aton(host) + struct.pack(">H", port) for host, port in peers
    )


class _TrackerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - stdlib naming
        query = urlsplit(self.path).query
        fields = {}
        for part in query.split("&"):
            key, _, value = part.partition("=")
            fields[key] = value
        self.server.seen.append(fields)
        info_hash = unquote_to_bytes(fields.get("info_hash", ""))
        if self.server.info_hash is not None and info_hash != self.server.info_hash:
            payload = {b"failure reason": b"wrong info hash"}
        else:
            payload = {
                b"interval": self.server.interval,
                b"peers": compact_peers(self.server.peers),
            }
        body = bencode.encode(payload)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence test output
        pass


class HttpTracker:
    """Tiny HTTP tracker used by the tests."""

    def __init__(self, peers, *, info_hash: bytes | None = None, interval: int = 1):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TrackerHandler)
        self.httpd.daemon_threads = True
        self.httpd.peers = list(peers)
        self.httpd.info_hash = info_hash
        self.httpd.interval = interval
        self.httpd.seen = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/announce"

    @property
    def seen(self):
        return self.httpd.seen

    def start(self) -> "HttpTracker":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def __enter__(self) -> "HttpTracker":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()


class UdpTracker(threading.Thread):
    """Minimal BEP 15 tracker for testing the UDP announce path."""

    def __init__(self, peers):
        super().__init__(daemon=True)
        self.peers = list(peers)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self._stop_event = threading.Event()
        self.announced: list[bytes] = []

    @property
    def url(self) -> str:
        return f"udp://127.0.0.1:{self.port}/announce"

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, address = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 16:
                continue
            action = struct.unpack(">I", data[8:12])[0]
            transaction_id = struct.unpack(">I", data[12:16])[0]
            if action == 0:
                response = struct.pack(">II", 0, transaction_id) + struct.pack(">Q", 0x1122334455667788)
            elif action == 1 and len(data) >= 98:
                self.announced.append(data[16:36])
                response = (
                    struct.pack(">II", 1, transaction_id)
                    + struct.pack(">III", 1, 0, 1)
                    + compact_peers(self.peers)
                )
            else:
                continue
            self.sock.sendto(response, address)

    def stop(self) -> None:
        self._stop_event.set()
        self.sock.close()
        self.join(timeout=2)

    def __enter__(self) -> "UdpTracker":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
