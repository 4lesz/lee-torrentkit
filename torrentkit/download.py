"""Download orchestration: trackers, peers, pieces and progress."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .dht import DEFAULT_BOOTSTRAP, DHTNode
from .metainfo import Magnet, Torrent
from .peers import LeechSession, MAX_PEX_PEERS, fetch_metadata, make_peer_id
from .pieces import TorrentState
from .storage import FileStorage
from .tracker import announce_all, collect_peers


# Connection attempts allowed on top of max_peers. Most addresses from DHT
# are dead and take connect_timeout to fail; they must not starve real peers.
MAX_HALF_OPEN = 30
# Addresses that never completed a handshake are dropped after this many tries.
MAX_DEAD_ATTEMPTS = 3
MAX_RETRY_DELAY = 600.0
# A peer that keeps us choked this long gives up its slot when others wait.
CHOKED_EVICT_AFTER = 90.0


class DownloadError(RuntimeError):
    pass


@dataclass
class Progress:
    downloaded: int
    total: int
    speed: float
    peers: int
    pieces_done: int
    pieces_total: int

    @property
    def fraction(self) -> float:
        return self.downloaded / self.total if self.total else 1.0


def parse_peer_address(text: str) -> tuple[str, int]:
    host, separator, port_text = text.strip().rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError(f"invalid peer address {text!r}, expected HOST:PORT")
    port = int(port_text)
    if not 0 < port < 65536:
        raise ValueError(f"invalid peer port {port}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, port


class Downloader:
    """Fetches one torrent from trackers/direct peers into ``save_dir``."""

    def __init__(
        self,
        torrent: Torrent,
        save_dir: str | Path,
        *,
        port: int = 6881,
        peer_id: bytes | None = None,
        extra_peers: Iterable[tuple[str, int]] = (),
        rarest_first: bool = False,
        max_peers: int = 100,
        announce_timeout: float = 10.0,
        connect_timeout: float = 8.0,
        # Peers that keep us choked only send keep-alives, about every two
        # minutes (BEP 3); a shorter timeout drops healthy connections.
        idle_timeout: float = 180.0,
        use_dht: bool = True,
        dht_bootstrap: Iterable[tuple[str, int]] | None = None,
        logger: logging.Logger | None = None,
    ):
        if not torrent.has_v1_hashes:
            raise DownloadError(
                "this torrent has no v1 piece hashes (BitTorrent v2 only); "
                "use a client with v2 support"
            )
        self.torrent = torrent
        self.save_dir = Path(save_dir)
        self.port = port
        self.peer_id = peer_id or make_peer_id()
        self.extra_peers = list(extra_peers)
        self.rarest_first = rarest_first
        self.max_peers = max_peers
        self.announce_timeout = announce_timeout
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.peer_retry_delay = 30.0
        self.use_dht = use_dht and not torrent.private
        self.dht_bootstrap = list(dht_bootstrap or DEFAULT_BOOTSTRAP)
        self._dht: DHTNode | None = None
        self._dht_lock = threading.Lock()
        self._finished = False
        self.logger = logger or logging.getLogger("torrentkit")

        self.storage: FileStorage | None = None
        self.state: TorrentState | None = None
        self._stop = threading.Event()
        self._peer_queue: queue.Queue = queue.Queue()
        self._seen: set[tuple[str, int]] = set()
        self._retry: dict[tuple[str, int], float] = {}
        self._failures: dict[tuple[str, int], int] = {}
        self._active: dict[tuple[str, int], LeechSession] = {}
        self._lock = threading.Lock()
        self._tracker_cb: Callable[[dict], None] | None = None
        self._wake_tracker = threading.Event()

    def run(
        self,
        progress_cb: Callable[[Progress], None] | None = None,
        stop_event: threading.Event | None = None,
        *,
        poll: float = 0.5,
        verify_cb: Callable[[int, int], None] | None = None,
        prepare_cb: Callable[[int, int], None] | None = None,
        tracker_cb: Callable[[dict], None] | None = None,
    ) -> bool:
        if self.state is not None:
            raise DownloadError("this Downloader instance has already been used")
        self._stop = stop_event or threading.Event()
        self._wake_tracker.clear()
        self._tracker_cb = tracker_cb
        self.storage = FileStorage(self.torrent, self.save_dir)
        try:
            if not self.storage.prepare(prepare_cb, self._stop):
                return False
            self.state = TorrentState(self.torrent, self.storage, rarest_first=self.rarest_first)
            self.logger.info("verifying existing data in %s", self.save_dir)
            self.state.load_existing(verify_cb, self._stop)
            if self._stop.is_set():
                return False

            if self.state.is_complete:
                self.logger.info("all pieces are already present")
                if progress_cb:
                    progress_cb(self._progress(0.0))
                return True

            self.add_peers(self.extra_peers)
            if self.use_dht:
                self.enable_dht()

            tracker_thread = threading.Thread(
                target=self._tracker_loop, name="torrentkit-tracker", daemon=True
            )
            tracker_thread.start()

            # Speed uses raw received bytes: with 16 MiB pieces, verified bytes
            # only move every few minutes and the UI would show 0 B/s.
            last_bytes = self.state.received
            last_time = time.monotonic()
            dht_next_query = last_time + 10.0
            speed = 0.0
            try:
                while not self.state.is_complete and not self._stop.is_set():
                    self._prune_peers()
                    self._evict_choked_peers()
                    self._start_peers()
                    now = time.monotonic()
                    dht = self._dht
                    if dht is not None and now >= dht_next_query:
                        dht.get_peers()
                        dht_next_query = now + 10.0
                    elapsed = now - last_time
                    if elapsed >= 0.2:
                        received = self.state.received
                        instant = (received - last_bytes) / elapsed
                        speed = instant if speed == 0.0 else 0.75 * speed + 0.25 * instant
                        last_bytes = received
                        last_time = now
                    if progress_cb:
                        progress_cb(self._progress(speed))
                    self._stop.wait(poll)
            finally:
                self._stop.set()
                self._stop_dht()
                self._shutdown_peers()
                tracker_thread.join(timeout=2.0)

            if self.state.is_complete:
                self._announce_completed()
                if progress_cb:
                    progress_cb(self._progress(speed))
                return True
            return False
        finally:
            # enable_dht() may run from another thread (e.g. the web UI adding
            # sources) during prepare/verify, so always tear the node down here.
            with self._dht_lock:
                self._finished = True
            self._stop_dht()
            self.storage.close()

    def add_peer(self, address: tuple[str, int]) -> None:
        host, port = address
        if not host or not 0 < port < 65536:
            return
        with self._lock:
            if address in self._seen:
                return
            self._seen.add(address)
            self._peer_queue.put(address)

    def add_peers(self, peers: Iterable[tuple[str, int]]) -> None:
        for peer in peers:
            self.add_peer(peer)

    def add_trackers(self, trackers: Iterable[str]) -> None:
        changed = False
        with self._lock:
            for tracker in trackers:
                if not tracker:
                    continue
                # Callers may already have appended the tracker to the shared
                # torrent object; still wake the loop so it is announced now.
                changed = True
                if tracker not in self.torrent.trackers:
                    self.torrent.trackers.append(tracker)
        if changed:
            self._wake_tracker.set()

    def enable_dht(self, enabled: bool = True) -> None:
        if not enabled or self.torrent.private:
            return
        with self._dht_lock:
            if self._dht is not None or self._finished or self._stop.is_set():
                return
            try:
                self._dht = DHTNode(
                    self.torrent.info_hash,
                    port=0,
                    announce_port=self.port,
                    bootstrap=self.dht_bootstrap,
                    on_peers=self.add_peers,
                    logger=self.logger,
                ).start()
            except OSError as exc:
                self._dht = None
                self.logger.warning("could not start DHT: %s", exc)
                return
            self._dht.get_peers()

    @property
    def dht_nodes(self) -> int:
        dht = self._dht
        return dht.node_count if dht is not None else 0

    def _stop_dht(self) -> None:
        with self._dht_lock:
            dht, self._dht = self._dht, None
        if dht is not None:
            dht.stop()

    def known_peers(self) -> list[tuple[str, int]]:
        with self._lock:
            return list(self._seen)[:MAX_PEX_PEERS]

    def _start_peers(self) -> None:
        assert self.state is not None
        while True:
            connected = sum(1 for session in self._active.values() if session.connected)
            half_open = len(self._active) - connected
            if connected >= self.max_peers or half_open >= MAX_HALF_OPEN:
                return
            with self._lock:
                now = time.monotonic()
                for address, due in list(self._retry.items()):
                    if due <= now:
                        self._retry.pop(address, None)
                        self._peer_queue.put(address)
            try:
                address = self._peer_queue.get_nowait()
            except queue.Empty:
                return
            if address in self._active:
                continue
            session = LeechSession(
                self.state,
                address[0],
                address[1],
                self.peer_id,
                self._stop,
                connect_timeout=self.connect_timeout,
                idle_timeout=self.idle_timeout,
                # BEP 27: private torrents must not use PEX.
                on_pex_peers=None if self.torrent.private else self.add_peers,
                pex_peer_provider=None if self.torrent.private else self.known_peers,
                logger=self.logger,
            )
            self._active[address] = session
            session.start()

    def _prune_peers(self) -> None:
        for address, session in list(self._active.items()):
            if session.is_alive():
                continue
            self._active.pop(address, None)
            if self.state is None or self.state.is_complete or self._stop.is_set():
                continue
            with self._lock:
                if session.ever_received:
                    failures = self._failures[address] = 0
                else:
                    failures = self._failures[address] = self._failures.get(address, 0) + 1
                    if not session.connected and failures >= MAX_DEAD_ATTEMPTS:
                        # Unreachable; tracker/DHT re-announcing it will not revive it.
                        continue
                delay = min(MAX_RETRY_DELAY, self.peer_retry_delay * (2 ** failures))
                self._retry[address] = time.monotonic() + delay

    def _evict_choked_peers(self) -> None:
        if self._peer_queue.empty():
            return
        connected = [session for session in self._active.values() if session.connected]
        if len(connected) < self.max_peers:
            return
        now = time.monotonic()
        stale = [
            session
            for session in connected
            if session.choked and now - session.choked_since > CHOKED_EVICT_AFTER
        ]
        # Free a few slots per pass; the longest-choked go first.
        stale.sort(key=lambda session: session.choked_since)
        for session in stale[: max(1, self.max_peers // 10)]:
            session.drop()

    def _shutdown_peers(self) -> None:
        sessions = list(self._active.values())
        for session in sessions:
            session.close()
        deadline = time.monotonic() + 3.0
        for session in sessions:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            session.join(timeout=remaining)
        self._active.clear()
        with self._lock:
            self._retry.clear()

    def _tracker_loop(self) -> None:
        assert self.state is not None
        event = "started"
        while not self._stop.is_set() and not self.state.is_complete:
            interval = 300.0
            trackers = list(self.torrent.trackers)
            if trackers:
                downloaded = self.state.downloaded
                left = max(0, self.torrent.total_length - downloaded)
                results = announce_all(
                    trackers,
                    info_hash=self.torrent.info_hash,
                    peer_id=self.peer_id,
                    port=self.port,
                    downloaded=downloaded,
                    left=left,
                    event=event,
                    numwant=80,
                    timeout=self.announce_timeout,
                )
                intervals = []
                ok = failed = peer_count = 0
                errors: list[str] = []
                for result in results:
                    if result.ok:
                        ok += 1
                        peer_count += len(result.peers)
                        self.add_peers(result.peers)
                        intervals.append(max(60.0, min(float(result.interval), 1800.0)))
                    else:
                        failed += 1
                        if result.failure:
                            errors.append(str(result.failure))
                        self.logger.debug("tracker %s failed: %s", result.tracker, result.failure)
                if self._tracker_cb:
                    try:
                        self._tracker_cb({"ok": ok, "failed": failed, "peers": peer_count, "errors": errors[:3]})
                    except Exception:
                        self.logger.debug("tracker callback failed", exc_info=True)
                if intervals:
                    interval = min(intervals)
                if not ok:
                    interval = min(interval, 30.0)
                elif peer_count == 0:
                    interval = min(interval, 60.0)
            elif self._dht is not None:
                # The main loop already queries the DHT every 10 seconds.
                interval = 30.0
            else:
                self._wake_tracker.wait(interval)
                self._wake_tracker.clear()
                continue
            event = None
            self._wake_tracker.wait(interval)
            self._wake_tracker.clear()

    def _announce_completed(self) -> None:
        if not self.torrent.trackers:
            return
        try:
            announce_all(
                self.torrent.trackers,
                info_hash=self.torrent.info_hash,
                peer_id=self.peer_id,
                port=self.port,
                downloaded=self.torrent.total_length,
                left=0,
                event="completed",
                numwant=0,
                timeout=min(5.0, self.announce_timeout),
                workers=4,
            )
        except Exception:
            pass

    def _progress(self, speed: float) -> Progress:
        assert self.state is not None
        snapshot = self.state.snapshot()
        return Progress(
            downloaded=snapshot["downloaded"],
            total=snapshot["total"],
            speed=speed,
            peers=len(self._active),
            pieces_done=snapshot["pieces_done"],
            pieces_total=snapshot["pieces_total"],
        )


def resolve_source(
    spec: str,
    *,
    extra_peers: Iterable[tuple[str, int]] = (),
    peer_id: bytes | None = None,
    port: int = 6881,
    timeout: float = 12.0,
    use_dht: bool = True,
    dht_bootstrap: Iterable[tuple[str, int]] | None = None,
    logger: logging.Logger | None = None,
) -> tuple[Torrent, Magnet | None]:
    """Load a torrent source, resolving magnet metadata if necessary."""
    logger = logger or logging.getLogger("torrentkit")
    text = spec.strip()
    extra = list(extra_peers)
    client_id = peer_id or make_peer_id()

    if text.lower().startswith("magnet:"):
        magnet = Magnet.parse(text)
        if magnet.info_hash is None:
            raise DownloadError("magnet URI has no usable info hash")
        if not magnet.has_v1_hash:
            raise DownloadError(
                "this magnet only carries a BitTorrent v2 hash; "
                "metadata exchange requires a v1 (btih) hash"
            )
        peers = list(extra)
        if use_dht:
            dht = None
            try:
                dht = DHTNode(
                    magnet.info_hash,
                    announce_port=port or 6881,
                    bootstrap=dht_bootstrap or DEFAULT_BOOTSTRAP,
                    logger=logger,
                ).start()
                dht.get_peers()
                peers.extend(dht.collect_peers(min(timeout, 8.0)))
            except OSError as exc:
                logger.debug("DHT bootstrap failed: %s", exc)
            finally:
                if dht is not None:
                    dht.stop()
        if magnet.trackers:
            results = announce_all(
                magnet.trackers,
                info_hash=magnet.info_hash,
                peer_id=client_id,
                port=port or 6881,
                left=magnet.xl or 0,
                event="started",
                numwant=80,
                timeout=min(10.0, timeout),
            )
            peers.extend(collect_peers(results))
        peers = list(dict.fromkeys(peers))
        if not peers:
            raise DownloadError("no peers found for this magnet after tracker/DHT lookup")
        info = fetch_metadata(magnet.info_hash, peers, client_id, timeout=timeout, logger=logger)
        torrent = Torrent.from_info(info, trackers=magnet.trackers, web_seeds=magnet.web_seeds)
        if torrent.info_hash != magnet.info_hash:
            raise DownloadError("metadata does not match the magnet info hash")
        return torrent, magnet

    path = Path(text)
    if not path.is_file():
        raise DownloadError(f"{text} is neither an existing .torrent file nor a magnet URI")
    return Torrent.from_file(path), None
