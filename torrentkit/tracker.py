"""Tracker announces over HTTP(S) and UDP (BEP 3, BEP 15, BEP 23)."""

from __future__ import annotations

import random
import socket
import struct
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote, quote_from_bytes, urlsplit

from . import bencode

USER_AGENT = "torrentkit/0.1.0"
MAX_RESPONSE = 4 * 1024 * 1024
UDP_PROTOCOL_ID = 0x41727101980
UDP_TIMEOUTS = (4.0, 8.0, 8.0)


class TrackerError(RuntimeError):
    pass


@dataclass
class AnnounceResult:
    tracker: str
    interval: int = 1800
    min_interval: int | None = None
    peers: list[tuple[str, int]] = field(default_factory=list)
    seeders: int = 0
    leechers: int = 0
    warning: str | None = None
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def _validate_ids(info_hash: bytes, peer_id: bytes) -> None:
    if not isinstance(info_hash, bytes) or len(info_hash) != 20:
        raise ValueError("info_hash must be 20 bytes")
    if not isinstance(peer_id, bytes) or len(peer_id) != 20:
        raise ValueError("peer_id must be 20 bytes")


def _decode_peers(value: object) -> list[tuple[str, int]]:
    peers: list[tuple[str, int]] = []
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        for offset in range(0, len(raw) - 5, 6):
            ip = ".".join(str(byte) for byte in raw[offset : offset + 4])
            port = struct.unpack(">H", raw[offset + 4 : offset + 6])[0]
            if port:
                peers.append((ip, port))
    elif isinstance(value, list):
        for entry in value:
            if not isinstance(entry, dict):
                continue
            raw_ip = entry.get(b"ip")
            port = entry.get(b"port")
            if not isinstance(raw_ip, bytes) or not isinstance(port, int):
                continue
            host = raw_ip.decode("utf-8", "replace")
            if host and 0 < port < 65536:
                peers.append((host, port))
    return peers


def _decode_peers6(value: object) -> list[tuple[str, int]]:
    peers: list[tuple[str, int]] = []
    if not isinstance(value, (bytes, bytearray)):
        return peers
    raw = bytes(value)
    for offset in range(0, len(raw) - 17, 18):
        packed = raw[offset : offset + 16]
        port = struct.unpack(">H", raw[offset + 16 : offset + 18])[0]
        if not port:
            continue
        host = socket.inet_ntop(socket.AF_INET6, packed)
        peers.append((host, port))
    return peers


def _build_query(
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str | None,
    numwant: int,
) -> str:
    values: list[tuple[str, bytes | str]] = [
        ("info_hash", info_hash),
        ("peer_id", peer_id),
        ("port", str(port)),
        ("uploaded", str(uploaded)),
        ("downloaded", str(downloaded)),
        ("left", str(left)),
        ("compact", "1"),
        ("numwant", str(numwant)),
    ]
    if event:
        values.append(("event", event))
    parts = []
    for key, value in values:
        if isinstance(value, bytes):
            encoded = quote_from_bytes(value, safe="")
        else:
            encoded = quote(value, safe="")
        parts.append(f"{key}={encoded}")
    return "&".join(parts)


def announce_http(
    tracker: str,
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int = 0,
    downloaded: int = 0,
    left: int = 0,
    event: str | None = None,
    numwant: int = 50,
    timeout: float = 10.0,
) -> AnnounceResult:
    _validate_ids(info_hash, peer_id)
    query = _build_query(
        info_hash=info_hash,
        peer_id=peer_id,
        port=port,
        uploaded=uploaded,
        downloaded=downloaded,
        left=left,
        event=event,
        numwant=numwant,
    )
    separator = "&" if "?" in tracker else "?"
    request = urllib.request.Request(
        tracker + separator + query,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )

    body: bytes
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise TrackerError("tracker response is too large")
    if not body:
        raise TrackerError("tracker returned an empty response")

    try:
        payload = bencode.decode(body.strip(), strict=False)
    except bencode.BencodeError as exc:
        raise TrackerError(f"tracker returned invalid bencode: {exc}") from exc
    if not isinstance(payload, dict):
        raise TrackerError("tracker response is not a dictionary")

    result = AnnounceResult(tracker=tracker)
    raw_failure = payload.get(b"failure reason")
    if isinstance(raw_failure, bytes):
        result.failure = raw_failure.decode("utf-8", "replace")
        return result
    raw_warning = payload.get(b"warning message")
    if isinstance(raw_warning, bytes):
        result.warning = raw_warning.decode("utf-8", "replace")

    interval = payload.get(b"interval")
    if isinstance(interval, int) and interval > 0:
        result.interval = interval
    min_interval = payload.get(b"min interval")
    if isinstance(min_interval, int) and min_interval > 0:
        result.min_interval = min_interval

    seen: set[tuple[str, int]] = set()
    for peer in _decode_peers(payload.get(b"peers")) + _decode_peers6(payload.get(b"peers6")):
        if peer not in seen:
            seen.add(peer)
            result.peers.append(peer)

    complete = payload.get(b"complete")
    incomplete = payload.get(b"incomplete")
    result.seeders = complete if isinstance(complete, int) else 0
    result.leechers = incomplete if isinstance(incomplete, int) else 0
    return result


def _udp_exchange(
    sock: socket.socket,
    address: tuple[str, int],
    payload: bytes,
    expected_action: int,
    transaction_id: int,
    timeouts: tuple[float, ...] = UDP_TIMEOUTS,
) -> bytes:
    for timeout in timeouts:
        sock.settimeout(timeout)
        sock.sendto(payload, address)
        try:
            while True:
                data, _ = sock.recvfrom(65536)
                if len(data) < 8:
                    continue
                action, received_id = struct.unpack(">II", data[:8])
                if received_id != transaction_id:
                    continue
                if action == 3:
                    message = data[8:].decode("utf-8", "replace")
                    raise TrackerError(f"tracker error: {message}")
                if action != expected_action:
                    raise TrackerError(f"unexpected UDP tracker action {action}")
                return data
        except socket.timeout:
            continue
    raise TrackerError("UDP tracker timed out")


def announce_udp(
    tracker: str,
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int = 0,
    downloaded: int = 0,
    left: int = 0,
    event: str | None = None,
    numwant: int = 50,
    timeout: float = 10.0,
) -> AnnounceResult:
    _validate_ids(info_hash, peer_id)
    parts = urlsplit(tracker)
    host = parts.hostname
    if not host:
        raise TrackerError("UDP tracker has no host")
    port_number = parts.port or 6969
    try:
        address = socket.getaddrinfo(host, port_number, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    except socket.gaierror as exc:
        raise TrackerError(f"cannot resolve tracker host {host}: {exc}") from exc

    events = {"completed": 1, "started": 2, "stopped": 3, None: 0}
    if event not in events:
        raise ValueError(f"invalid announce event {event!r}")
    event_code = events[event]
    downloaded = int(downloaded) & 0xFFFFFFFFFFFFFFFF
    left = int(left) & 0xFFFFFFFFFFFFFFFF
    uploaded = int(uploaded) & 0xFFFFFFFFFFFFFFFF
    timeouts = (max(1.0, timeout / 2), timeout, timeout)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        transaction_id = random.getrandbits(32)
        request = struct.pack(">QII", UDP_PROTOCOL_ID, 0, transaction_id)
        response = _udp_exchange(sock, address, request, 0, transaction_id, timeouts)
        if len(response) < 16:
            raise TrackerError("short UDP connect response")
        connection_id = int.from_bytes(response[8:16], "big")

        transaction_id = random.getrandbits(32)
        announce = struct.pack(">QII", connection_id, 1, transaction_id)
        announce += info_hash + peer_id
        announce += struct.pack(">QQQ", downloaded, left, uploaded)
        # event, IP address, key, num_want, port
        announce += struct.pack(">iIIIH", event_code, 0, transaction_id, numwant, port)
        response = _udp_exchange(sock, address, announce, 1, transaction_id, timeouts)

    if len(response) < 20:
        raise TrackerError("short UDP announce response")
    interval, leechers, seeders = struct.unpack(">III", response[8:20])
    result = AnnounceResult(
        tracker=tracker,
        interval=interval or 1800,
        seeders=seeders,
        leechers=leechers,
        peers=_decode_peers(response[20:]),
    )
    return result


def announce(
    tracker: str,
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int = 0,
    downloaded: int = 0,
    left: int = 0,
    event: str | None = None,
    numwant: int = 50,
    timeout: float = 10.0,
) -> AnnounceResult:
    """Announce to a single tracker, never raising for tracker-side errors."""
    scheme = urlsplit(tracker).scheme.lower()
    try:
        if scheme in ("http", "https"):
            return announce_http(
                tracker,
                info_hash=info_hash,
                peer_id=peer_id,
                port=port,
                uploaded=uploaded,
                downloaded=downloaded,
                left=left,
                event=event,
                numwant=numwant,
                timeout=timeout,
            )
        if scheme == "udp":
            return announce_udp(
                tracker,
                info_hash=info_hash,
                peer_id=peer_id,
                port=port,
                uploaded=uploaded,
                downloaded=downloaded,
                left=left,
                event=event,
                numwant=numwant,
                timeout=timeout,
            )
        return AnnounceResult(tracker=tracker, failure=f"unsupported tracker scheme {scheme!r}")
    except Exception as exc:  # tracker failures are reported, not raised
        return AnnounceResult(tracker=tracker, failure=str(exc) or type(exc).__name__)


def announce_all(
    trackers: list[str] | tuple[str, ...],
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int = 0,
    downloaded: int = 0,
    left: int = 0,
    event: str | None = None,
    numwant: int = 50,
    timeout: float = 10.0,
    workers: int = 8,
) -> list[AnnounceResult]:
    """Announce to every tracker concurrently and return per-tracker results."""
    unique = list(dict.fromkeys(trackers))
    if not unique:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(unique))) as executor:
        futures = [
            executor.submit(
                announce,
                tracker,
                info_hash=info_hash,
                peer_id=peer_id,
                port=port,
                uploaded=uploaded,
                downloaded=downloaded,
                left=left,
                event=event,
                numwant=numwant,
                timeout=timeout,
            )
            for tracker in unique
        ]
        return [future.result() for future in futures]


def collect_peers(results: list[AnnounceResult]) -> list[tuple[str, int]]:
    peers: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for result in results:
        for peer in result.peers:
            if peer not in seen:
                seen.add(peer)
                peers.append(peer)
    return peers
