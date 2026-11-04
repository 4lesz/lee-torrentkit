"""A small local web UI for torrentkit.

The server is intentionally local-first: it binds to 127.0.0.1 by default, has
no authentication, and confines downloads to the configured directory unless
external folders are explicitly allowed.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import string
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .download import DownloadError, Downloader, parse_peer_address, resolve_source
from .metainfo import Magnet, MetainfoError, Torrent
from .peers import make_peer_id
from .sockets import configure_reuse, is_address_in_use

LOG = logging.getLogger("torrentkit.web")

STATIC_DIR = Path(__file__).resolve().parent / "webui"
STATIC_FILES = {"index.html", "style.css", "app.js"}
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

MAX_BODY = 32 * 1024 * 1024
MAX_TORRENT_BYTES = 16 * 1024 * 1024
FILE_PREVIEW_LIMIT = 300
MAX_PEERS = 200
DEFAULT_MAX_PEERS = 100
ANNOUNCE_PORT = 6881
DEFAULT_MAX_ACTIVE = 2

ACTIVE_STATES = {"queued", "preparing", "resolving", "verifying", "downloading"}

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# -- helpers ---------------------------------------------------------------


def _decode_torrent(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "缺少 torrent 数据")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, "torrent 数据不是合法的 base64") from exc
    if not raw:
        raise ApiError(400, "torrent 数据为空")
    if len(raw) > MAX_TORRENT_BYTES:
        raise ApiError(413, "torrent 文件过大（上限 16 MiB）")
    return raw


def _parse_peers(value: Any) -> list[tuple[str, int]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ApiError(400, "peers 必须是字符串数组")
    if len(value) > MAX_PEERS:
        raise ApiError(400, f"peers 最多 {MAX_PEERS} 个")
    peers: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, str):
            raise ApiError(400, "peers 必须是字符串数组")
        text = item.strip()
        if not text:
            continue
        try:
            peers.append(parse_peer_address(text))
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
    return list(dict.fromkeys(peers))


def _parse_trackers(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ApiError(400, "trackers 必须是字符串数组")
    if len(value) > 100:
        raise ApiError(400, "trackers 最多 100 个")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ApiError(400, "trackers 必须是字符串数组")
        url = item.strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://", "udp://")):
            raise ApiError(400, f"不支持的 Tracker 地址：{url}")
        result.append(url)
    return list(dict.fromkeys(result))


def _bool_option(value: Any, default: bool, name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ApiError(400, f"{name} 必须是布尔值")
    return value


def _int_option(value: Any, default: int, minimum: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{name} 必须是整数") from exc
    if not minimum <= number <= maximum:
        raise ApiError(400, f"{name} 必须在 {minimum}..{maximum} 之间")
    return number


def _file_rows(torrent: Torrent) -> tuple[list[dict], int]:
    rows = [
        {"path": entry.display(torrent.name, torrent.is_single_file), "length": entry.length}
        for entry in torrent.files[:FILE_PREVIEW_LIMIT]
    ]
    return rows, len(torrent.files)


def _summarize_torrent(torrent: Torrent) -> dict:
    files, files_total = _file_rows(torrent)
    return {
        "ok": True,
        "source": "torrent",
        "resolved": True,
        "name": torrent.name,
        "info_hash": torrent.info_hash.hex(),
        "info_hash_v2": torrent.info_hash_v2.hex() if torrent.info_hash_v2 else None,
        "total_length": torrent.total_length,
        "piece_length": torrent.piece_length,
        "piece_count": torrent.piece_count,
        "private": torrent.private,
        "trackers": torrent.trackers,
        "web_seeds": torrent.web_seeds,
        "comment": torrent.comment,
        "files": files,
        "files_total": files_total,
        "magnet": torrent.magnet_uri(),
        "v2_only": not torrent.has_v1_hashes,
        "warning": None if torrent.has_v1_hashes else "纯 BitTorrent v2 种子暂不支持下载，仅能查看信息。",
    }


def _summarize_magnet(text: str, magnet: Magnet) -> dict:
    if not magnet.has_v1_hash:
        warning = "该磁力链接只包含 BitTorrent v2 哈希，暂不支持下载。"
    elif magnet.trackers or magnet.web_seeds:
        warning = "文件列表和分片信息会在开始下载时通过 peer 获取。"
    else:
        warning = "该磁力链接没有 tracker，开始下载前请先在高级选项里填写直连 peer。"
    return {
        "ok": True,
        "source": "magnet",
        "resolved": False,
        "name": magnet.display_name,
        "info_hash": magnet.info_hash.hex() if magnet.info_hash else None,
        "info_hash_v2": magnet.info_hash_v2.hex() if magnet.info_hash_v2 else None,
        "total_length": magnet.xl or 0,
        "piece_length": 0,
        "piece_count": 0,
        "private": False,
        "trackers": magnet.trackers,
        "web_seeds": magnet.web_seeds,
        "comment": None,
        "files": [],
        "files_total": 0,
        "magnet": text,
        "v2_only": not magnet.has_v1_hash,
        "warning": warning,
    }


def _open_path(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        raise ApiError(500, "无法打开文件管理器") from exc


def _list_roots() -> list[dict]:
    if sys.platform.startswith("win"):
        roots = []
        for letter in string.ascii_uppercase:
            candidate = Path(f"{letter}:\\")
            try:
                if candidate.exists():
                    roots.append({"name": f"{letter}:\\", "path": str(candidate)})
            except OSError:
                continue
        return roots
    roots = [{"name": "/", "path": "/"}]
    home = Path.home()
    if str(home) != "/":
        roots.append({"name": str(home), "path": str(home)})
    return roots


# -- job management --------------------------------------------------------


@dataclass
class Job:
    id: str
    name: str
    info_hash: str
    info_hash_v2: str | None
    total_length: int
    source: str
    magnet: str
    files: list[dict]
    files_total: int
    trackers: list[str]
    save_root: Path
    stop_event: threading.Event
    created_at: float
    state: str = "queued"
    downloaded: int = 0
    speed: float = 0.0
    peer_connections: int = 0
    pieces_done: int = 0
    pieces_total: int = 0
    verify_done: int = 0
    verify_total: int = 0
    error: str | None = None
    finished_at: float | None = None
    thread: threading.Thread | None = None
    torrent: Torrent | None = None
    magnet_text: str | None = None
    rarest_first: bool = False
    max_peers: int = DEFAULT_MAX_PEERS
    prepare_done: int = 0
    prepare_total: int = 0
    tracker_ok: int = 0
    tracker_failed: int = 0
    tracker_peers: int = 0
    tracker_error: str | None = None
    use_dht: bool = True
    peer_addresses: list[tuple[str, int]] = field(default_factory=list)
    downloader: Downloader | None = None


class JobManager:
    def __init__(
        self,
        root: Path,
        announce_port: int = ANNOUNCE_PORT,
        logger=None,
        max_active: int = DEFAULT_MAX_ACTIVE,
        allow_external_dirs: bool = True,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.announce_port = announce_port
        self.log = logger or LOG
        self.max_active = max(1, int(max_active))
        self.allow_external_dirs = allow_external_dirs
        self._jobs: dict[str, Job] = {}
        self._running: set[str] = set()
        self._shutting_down = False
        self._lock = threading.RLock()

    # -- preview ----------------------------------------------------------

    def preview(self, payload: dict) -> dict:
        torrent_value = payload.get("torrent")
        magnet_value = payload.get("magnet")
        has_torrent = isinstance(torrent_value, str) and bool(torrent_value.strip())
        has_magnet = isinstance(magnet_value, str) and bool(magnet_value.strip())
        if has_torrent and has_magnet:
            raise ApiError(400, "一次只能解析一种来源")
        if has_torrent:
            torrent = self._load_torrent(torrent_value)
            return _summarize_torrent(torrent)
        if has_magnet:
            text = magnet_value.strip()
            try:
                magnet = Magnet.parse(text)
            except MetainfoError as exc:
                raise ApiError(400, f"无法解析磁力链接：{exc}") from exc
            return _summarize_magnet(text, magnet)
        raise ApiError(400, "请提供 torrent 文件或磁力链接")

    def _load_torrent(self, value: str) -> Torrent:
        raw = _decode_torrent(value)
        try:
            return Torrent.from_bytes(raw)
        except MetainfoError as exc:
            raise ApiError(400, f"无法解析种子：{exc}") from exc

    # -- directories ------------------------------------------------------

    def _resolve_directory(self, value: Any, *, must_exist: bool = False) -> Path:
        if value is None or not str(value).strip():
            return self.root.resolve()
        if not isinstance(value, str):
            raise ApiError(400, "下载目录必须是字符串")
        text = os.path.expandvars(value.strip().strip('"').strip("'"))
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ApiError(400, "下载目录必须是绝对路径")
        try:
            path = path.resolve()
        except (OSError, RuntimeError) as exc:
            raise ApiError(400, f"无法解析目录：{exc}") from exc
        if not self.allow_external_dirs:
            root = self.root.resolve()
            if path != root and root not in path.parents:
                raise ApiError(400, f"下载目录必须位于 {root} 内")
        if path.exists():
            if not path.is_dir():
                raise ApiError(400, "路径不是文件夹")
        elif must_exist:
            raise ApiError(404, "目录不存在")
        return path

    def list_directory(self, value: str | None) -> dict:
        path = self._resolve_directory(value, must_exist=True)
        try:
            entries = list(path.iterdir())
        except OSError as exc:
            raise ApiError(403, f"无法读取目录：{exc}") from exc
        dirs = []
        truncated = False
        for entry in sorted(entries, key=lambda item: item.name.lower()):
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            dirs.append({"name": entry.name, "path": str(entry)})
            if len(dirs) >= 500:
                truncated = True
                break
        parent = path.parent if path.parent != path else None
        return {
            "ok": True,
            "path": str(path),
            "parent": str(parent) if parent is not None else None,
            "dirs": dirs,
            "roots": _list_roots(),
            "writable": os.access(path, os.W_OK),
            "truncated": truncated,
            "allow_external_dirs": self.allow_external_dirs,
        }

    def make_directory(self, parent_value: str | None, name: Any) -> dict:
        parent = self._resolve_directory(parent_value, must_exist=True)
        if not isinstance(name, str):
            raise ApiError(400, "文件夹名称不合法")
        name = name.strip()
        if not name or name in (".", "..") or len(name) > 120:
            raise ApiError(400, "文件夹名称不合法")
        if any(ch in name for ch in '\\/:*?"<>|') or any(ord(ch) < 32 for ch in name):
            raise ApiError(400, "文件夹名称不合法")
        try:
            target = (parent / name).resolve()
        except (OSError, RuntimeError) as exc:
            raise ApiError(400, f"无法创建文件夹：{exc}") from exc
        if target.parent != parent:
            raise ApiError(400, "文件夹名称不合法")
        if target.exists():
            if target.is_dir():
                return {"ok": True, "path": str(target)}
            raise ApiError(409, "同名文件已存在")
        try:
            target.mkdir()
        except OSError as exc:
            raise ApiError(400, f"无法创建文件夹：{exc}") from exc
        return {"ok": True, "path": str(target)}

    # -- jobs -------------------------------------------------------------

    def create_job(self, payload: dict) -> dict:
        torrent_value = payload.get("torrent")
        magnet_value = payload.get("magnet")
        has_torrent = isinstance(torrent_value, str) and bool(torrent_value.strip())
        has_magnet = isinstance(magnet_value, str) and bool(magnet_value.strip())
        if has_torrent and has_magnet:
            raise ApiError(400, "一次只能提交一种来源")
        peers = _parse_peers(payload.get("peers"))
        trackers = _parse_trackers(payload.get("trackers"))
        use_dht = _bool_option(payload.get("use_dht"), True, "use_dht")
        rarest_first = bool(payload.get("rarest_first"))
        max_peers = _int_option(
            payload.get("max_peers"), DEFAULT_MAX_PEERS, 1, MAX_PEERS, "max_peers"
        )
        save_root = self._resolve_directory(payload.get("save_dir"))
        job_id = uuid.uuid4().hex[:12]
        now = time.time()

        if has_torrent:
            torrent = self._load_torrent(torrent_value)
            if not torrent.has_v1_hashes:
                raise ApiError(400, "纯 BitTorrent v2 种子暂不支持下载")
            files, files_total = _file_rows(torrent)
            job = Job(
                id=job_id,
                name=torrent.name,
                info_hash=torrent.info_hash.hex(),
                info_hash_v2=torrent.info_hash_v2.hex() if torrent.info_hash_v2 else None,
                total_length=torrent.total_length,
                source="torrent",
                magnet=torrent.magnet_uri(),
                files=files,
                files_total=files_total,
                trackers=list(dict.fromkeys(torrent.trackers + trackers)),
                save_root=save_root,
                stop_event=threading.Event(),
                created_at=now,
                pieces_total=torrent.piece_count,
                torrent=torrent,
                peer_addresses=peers,
                rarest_first=rarest_first,
                max_peers=max_peers,
                use_dht=use_dht and not torrent.private,
            )
        elif has_magnet:
            text = magnet_value.strip()
            try:
                magnet = Magnet.parse(text)
            except MetainfoError as exc:
                raise ApiError(400, f"无法解析磁力链接：{exc}") from exc
            if magnet.info_hash is None:
                raise ApiError(400, "磁力链接缺少可用的 info hash")
            if not magnet.has_v1_hash:
                raise ApiError(400, "纯 BitTorrent v2 磁力链接暂不支持下载")
            job = Job(
                id=job_id,
                name=magnet.display_name,
                info_hash=magnet.info_hash.hex(),
                info_hash_v2=magnet.info_hash_v2.hex() if magnet.info_hash_v2 else None,
                total_length=magnet.xl or 0,
                source="magnet",
                magnet=text,
                files=[],
                files_total=0,
                trackers=list(dict.fromkeys(magnet.trackers + trackers)),
                save_root=save_root,
                stop_event=threading.Event(),
                created_at=now,
                magnet_text=text,
                peer_addresses=peers,
                rarest_first=rarest_first,
                max_peers=max_peers,
                use_dht=use_dht,
            )
        else:
            raise ApiError(400, "缺少 torrent 或 magnet")

        with self._lock:
            duplicate = next(
                (
                    existing
                    for existing in self._jobs.values()
                    if existing.info_hash == job.info_hash
                    and existing.state in ACTIVE_STATES
                ),
                None,
            )
            if duplicate is not None:
                raise ApiError(409, "相同 info hash 的任务已在队列中，请先停止或等待完成")
            self._jobs[job_id] = job
        self._schedule()
        with self._lock:
            return self._snapshot(self._get(job_id))

    def _schedule(self) -> None:
        """Start queued jobs while there is a free concurrency slot."""
        starts: list[threading.Thread] = []
        with self._lock:
            if self._shutting_down:
                return
            while len(self._running) < self.max_active:
                candidates = [
                    job
                    for job in self._jobs.values()
                    if job.state == "queued" and job.thread is None
                ]
                if not candidates:
                    break
                job = min(candidates, key=lambda item: item.created_at)
                if job.stop_event.is_set():
                    job.state = "stopped"
                    job.finished_at = time.time()
                    continue
                thread = threading.Thread(
                    target=self._run_job,
                    args=(job,),
                    name=f"torrentkit-job-{job.id}",
                    daemon=True,
                )
                job.thread = thread
                self._running.add(job.id)
                starts.append(thread)
        for thread in starts:
            thread.start()

    def _run_job(self, job: Job) -> None:
        peer_id = make_peer_id()
        try:
            torrent = job.torrent
            if torrent is None:
                assert job.magnet_text is not None
                self._update(job, state="resolving")
                torrent, _ = resolve_source(
                    job.magnet_text,
                    extra_peers=job.peer_addresses,
                    peer_id=peer_id,
                    port=self.announce_port,
                    use_dht=job.use_dht,
                    logger=self.log,
                )
                if job.stop_event.is_set():
                    self._finish(job, "stopped")
                    return
                files, files_total = _file_rows(torrent)
                with self._lock:
                    job.torrent = torrent
                    job.name = torrent.name
                    job.info_hash = torrent.info_hash.hex()
                    job.info_hash_v2 = (
                        torrent.info_hash_v2.hex() if torrent.info_hash_v2 else None
                    )
                    job.total_length = torrent.total_length
                    job.files = files
                    job.files_total = files_total
                    for tracker in job.trackers:
                        if tracker not in torrent.trackers:
                            torrent.trackers.append(tracker)
                    job.trackers = torrent.trackers
                    job.use_dht = job.use_dht and not torrent.private
                    job.magnet = torrent.magnet_uri()
                    job.pieces_total = torrent.piece_count

            if not torrent.trackers and not job.peer_addresses and not job.use_dht:
                raise DownloadError(
                    "这个种子没有 tracker，也没有直连 peer；请在高级选项里填写 Peer 后再试"
                )

            with self._lock:
                job.state = "preparing"
                job.prepare_done = 0
                job.prepare_total = len(torrent.files)
                job.verify_done = 0
                job.verify_total = torrent.piece_count
                job.tracker_ok = 0
                job.tracker_failed = 0
                job.tracker_peers = 0
                job.tracker_error = None

            downloader = Downloader(
                torrent,
                job.save_root,
                port=self.announce_port,
                peer_id=peer_id,
                extra_peers=job.peer_addresses,
                rarest_first=job.rarest_first,
                max_peers=job.max_peers,
                use_dht=job.use_dht,
                logger=self.log,
            )
            with self._lock:
                job.downloader = downloader

            def on_prepare(done: int, total: int) -> None:
                with self._lock:
                    if job.state in ("stopped", "error", "completed"):
                        return
                    job.state = "preparing"
                    job.prepare_done = done
                    job.prepare_total = total

            def on_verify(done: int, total: int) -> None:
                with self._lock:
                    if job.state in ("stopped", "error", "completed"):
                        return
                    job.state = "verifying"
                    job.verify_done = done
                    job.verify_total = total

            def on_progress(progress) -> None:
                with self._lock:
                    job.downloaded = progress.downloaded
                    job.speed = progress.speed
                    job.peer_connections = progress.peers
                    job.pieces_done = progress.pieces_done
                    job.pieces_total = progress.pieces_total
                    if job.state in ("preparing", "resolving", "verifying", "downloading"):
                        job.state = "downloading"

            def on_trackers(summary: dict) -> None:
                with self._lock:
                    if job.state in ("stopped", "error", "completed"):
                        return
                    job.tracker_ok = int(summary.get("ok") or 0)
                    job.tracker_failed = int(summary.get("failed") or 0)
                    job.tracker_peers = int(summary.get("peers") or 0)
                    errors = summary.get("errors") or []
                    job.tracker_error = str(errors[0])[:300] if errors else None

            ok = downloader.run(
                on_progress,
                job.stop_event,
                verify_cb=on_verify,
                prepare_cb=on_prepare,
                tracker_cb=on_trackers,
            )
            if ok:
                with self._lock:
                    job.downloaded = job.total_length
                    job.pieces_done = job.pieces_total
                    job.verify_done = job.verify_total
                self._finish(job, "completed")
            elif job.stop_event.is_set():
                self._finish(job, "stopped")
            else:
                self._finish(job, "error", "下载未完成（没有可用 peer 或连接中断）")
        except Exception as exc:
            LOG.debug("job %s failed", job.id, exc_info=True)
            self._finish(job, "error", str(exc) or type(exc).__name__)
        finally:
            self._on_job_finished(job)

    def _update(self, job: Job, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(job, key, value)

    def _on_job_finished(self, job: Job) -> None:
        with self._lock:
            self._running.discard(job.id)
            job.thread = None
            job.downloader = None
        self._schedule()

    def _finish(self, job: Job, state: str, error: str | None = None) -> None:
        with self._lock:
            job.state = state
            job.error = error
            job.speed = 0.0
            job.peer_connections = 0
            job.finished_at = time.time()

    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return [self._snapshot(job) for job in jobs]

    def _get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ApiError(404, "任务不存在")
        return job

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            return self._snapshot(self._get(job_id))

    def add_sources(self, job_id: str, payload: dict) -> dict:
        return self._add_sources_impl(job_id, payload)

    def _add_sources_impl(self, job_id: str, payload: dict) -> dict:
        job = self._get(job_id)
        if job.state not in ACTIVE_STATES or job.stop_event.is_set():
            raise ApiError(409, "只有排队中或运行中的任务可以补充 Peer/Tracker")
        added_peers = _parse_peers(payload.get("peers"))
        added_trackers = _parse_trackers(payload.get("trackers"))
        use_dht = _bool_option(payload.get("use_dht"), job.use_dht, "use_dht")
        with self._lock:
            for peer in added_peers:
                if peer not in job.peer_addresses:
                    job.peer_addresses.append(peer)
            for tracker in added_trackers:
                if tracker not in job.trackers:
                    job.trackers.append(tracker)
                    if job.torrent is not None and tracker not in job.torrent.trackers:
                        job.torrent.trackers.append(tracker)
            job.use_dht = use_dht and not (job.torrent.private if job.torrent else False)
            downloader = job.downloader
            if job.magnet_text and added_trackers:
                magnet = Magnet.parse(job.magnet_text)
                magnet.trackers = list(dict.fromkeys(magnet.trackers + added_trackers))
                job.magnet_text = magnet.uri
                job.magnet = magnet.uri
        if downloader is not None:
            downloader.add_peers(added_peers)
            downloader.add_trackers(added_trackers)
            downloader.enable_dht(job.use_dht)
        with self._lock:
            return self._snapshot(job)

    def stop_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._get(job_id)
            job.stop_event.set()
            if job.thread is None and job.state == "queued":
                job.state = "stopped"
                job.finished_at = time.time()
            return self._snapshot(job)

    def remove_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._get(job_id)
            if job.thread is not None and job.thread.is_alive():
                raise ApiError(409, "任务仍在运行，请先停止后再移除")
            self._jobs.pop(job_id, None)
        return {"ok": True}

    def reveal_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._get(job_id)
            target = (job.save_root / job.name).resolve()
            root = job.save_root.resolve()
        if target != root and root not in target.parents:
            raise ApiError(400, "目标路径不合法")
        if not target.exists():
            raise ApiError(404, "下载文件还不存在")
        _open_path(target)
        return {"ok": True}

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
            jobs = list(self._jobs.values())
        for job in jobs:
            job.stop_event.set()
            if job.thread is None and job.state in ACTIVE_STATES:
                with self._lock:
                    job.state = "stopped"
                    job.finished_at = time.time()
        deadline = time.monotonic() + 3.0
        for job in jobs:
            thread = job.thread
            if thread is None or not thread.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _snapshot(self, job: Job) -> dict:
        total = job.total_length
        if total > 0:
            progress = min(1.0, max(0.0, job.downloaded / total))
        else:
            progress = 1.0 if job.state == "completed" else 0.0
        now = time.time()
        elapsed = max(0.0, (job.finished_at or now) - job.created_at)
        queue_position = 0
        if job.state == "queued":
            queued = sorted(
                (item for item in self._jobs.values() if item.state == "queued"),
                key=lambda item: item.created_at,
            )
            for position, item in enumerate(queued, 1):
                if item.id == job.id:
                    queue_position = position
                    break
        return {
            "id": job.id,
            "name": job.name,
            "info_hash": job.info_hash,
            "info_hash_v2": job.info_hash_v2,
            "total_length": total,
            "downloaded": job.downloaded,
            "progress": progress,
            "speed": round(job.speed, 1),
            "peers": job.peer_connections,
            "peer_addresses": [f"{host}:{port}" for host, port in job.peer_addresses],
            "use_dht": job.use_dht,
            "pieces_done": job.pieces_done,
            "pieces_total": job.pieces_total,
            "verify_done": job.verify_done,
            "verify_total": job.verify_total,
            "prepare_done": job.prepare_done,
            "prepare_total": job.prepare_total,
            "tracker_ok": job.tracker_ok,
            "tracker_failed": job.tracker_failed,
            "tracker_peers": job.tracker_peers,
            "tracker_error": job.tracker_error,
            "queue_position": queue_position,
            "state": job.state,
            "error": job.error,
            "source": job.source,
            "magnet": job.magnet,
            "trackers": job.trackers,
            "files": job.files,
            "files_total": job.files_total,
            "save_path": str(job.save_root / job.name),
            "created_at": job.created_at,
            "finished_at": job.finished_at,
            "elapsed": round(elapsed, 1),
        }


# -- HTTP layer ------------------------------------------------------------

JOB_ID = r"([0-9a-f]{6,32})"
JOB_PATH = re.compile(rf"/api/jobs/{JOB_ID}")
JOB_STOP_PATH = re.compile(rf"/api/jobs/{JOB_ID}/stop")
JOB_REVEAL_PATH = re.compile(rf"/api/jobs/{JOB_ID}/reveal")
JOB_SOURCES_PATH = re.compile(rf"/api/jobs/{JOB_ID}/sources")


def _make_handler(manager: JobManager):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"torrentkit-web/{__version__}"
        timeout = 30

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            self._body_consumed = False
            try:
                self._route(method)
            finally:
                self._discard_unread_body()

        def _discard_unread_body(self) -> None:
            # Routes such as /stop ignore their body. Leaving it unread breaks
            # keep-alive (it is parsed as the next request) and on Windows the
            # close turns into a reset the client sees as ConnectionAborted.
            if self._body_consumed:
                return
            self._body_consumed = True
            header = self.headers.get("Content-Length")
            if not header:
                return
            try:
                length = int(header)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.close_connection = True
                return
            try:
                self.rfile.read(length)
            except OSError:
                self.close_connection = True

        def _route(self, method: str) -> None:
            try:
                if method == "GET":
                    self._route_get()
                elif method == "POST":
                    self._route_post()
                else:
                    self._route_delete()
            except ApiError as exc:
                self._json(exc.status, {"ok": False, "error": exc.message})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                LOG.exception("unhandled error while serving %s %s", method, self.path)
                try:
                    self._json(500, {"ok": False, "error": "服务器内部错误"})
                except Exception:
                    pass

        def _route_get(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
                return
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name not in STATIC_FILES:
                    raise ApiError(404, "not found")
                self._serve_static(name)
                return
            if path == "/api/health":
                self._json(
                    200,
                    {
                        "ok": True,
                        "version": __version__,
                        "download_dir": str(manager.root.resolve()),
                        "allow_external_dirs": manager.allow_external_dirs,
                    },
                )
                return
            if path == "/api/fs":
                query = parse_qs(urlsplit(self.path).query)
                target = query.get("path", [None])[0]
                self._json(200, manager.list_directory(target))
                return
            if path == "/api/jobs":
                self._json(
                    200,
                    {
                        "ok": True,
                        "jobs": manager.list_jobs(),
                        "download_dir": str(manager.root.resolve()),
                    },
                )
                return
            match = JOB_PATH.fullmatch(path)
            if match:
                self._json(200, {"ok": True, "job": manager.get_job(match.group(1))})
                return
            raise ApiError(404, "not found")

        def _route_post(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/parse":
                payload = self._read_json()
                self._json(200, manager.preview(payload))
                return
            if path == "/api/fs/mkdir":
                payload = self._read_json()
                self._json(
                    200,
                    manager.make_directory(payload.get("parent"), payload.get("name")),
                )
                return
            if path == "/api/jobs":
                payload = self._read_json()
                job = manager.create_job(payload)
                self._json(201, {"ok": True, "job": job})
                return
            match = JOB_STOP_PATH.fullmatch(path)
            if match:
                job = manager.stop_job(match.group(1))
                self._json(200, {"ok": True, "job": job})
                return
            match = JOB_REVEAL_PATH.fullmatch(path)
            if match:
                self._json(200, manager.reveal_job(match.group(1)))
                return
            match = JOB_SOURCES_PATH.fullmatch(path)
            if match:
                payload = self._read_json()
                self._json(200, manager.add_sources(match.group(1), payload))
                return
            raise ApiError(404, "not found")

        def _route_delete(self) -> None:
            path = urlsplit(self.path).path
            match = JOB_PATH.fullmatch(path)
            if match:
                self._json(200, manager.remove_job(match.group(1)))
                return
            raise ApiError(404, "not found")

        def _read_json(self) -> dict:
            if self.headers.get_content_type() != "application/json":
                raise ApiError(415, "Content-Type 必须是 application/json")
            header = self.headers.get("Content-Length")
            if header is None:
                raise ApiError(411, "缺少 Content-Length")
            try:
                length = int(header)
            except ValueError as exc:
                raise ApiError(400, "Content-Length 不合法") from exc
            if length < 0:
                raise ApiError(400, "Content-Length 不合法")
            if length > MAX_BODY:
                raise ApiError(413, "请求体过大")
            raw = self.rfile.read(length) if length else b""
            self._body_consumed = True
            if not raw:
                raise ApiError(400, "请求体为空")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, "请求体不是合法 JSON") from exc
            if not isinstance(payload, dict):
                raise ApiError(400, "请求体必须是 JSON 对象")
            return payload

        def _serve_static(self, name: str) -> None:
            path = STATIC_DIR / name
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ApiError(404, "not found") from exc
            content_type = STATIC_TYPES.get(path.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            LOG.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


class _Server(ThreadingHTTPServer):
    # HTTPServer sets SO_REUSEADDR, which on Windows lets a second instance
    # bind the same port; configure_reuse() picks the safe option per OS.
    allow_reuse_address = False

    def server_bind(self) -> None:
        configure_reuse(self.socket)
        super().server_bind()


class WebApp:
    """Owns the HTTP server and the job manager; usable from tests."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        announce_port: int = ANNOUNCE_PORT,
        max_active: int = DEFAULT_MAX_ACTIVE,
        allow_external_dirs: bool | None = None,
        logger=None,
    ):
        self.host = host
        if allow_external_dirs is None:
            allow_external_dirs = host in LOOPBACK_HOSTS
        self.manager = JobManager(
            Path(root),
            announce_port=announce_port,
            logger=logger,
            max_active=max_active,
            allow_external_dirs=allow_external_dirs,
        )
        try:
            self.httpd = _Server((host, port), _make_handler(self.manager))
        except OSError as exc:
            if is_address_in_use(exc):
                raise OSError(
                    exc.errno,
                    f"port {port} on {host} is already in use "
                    "(is another torrentkit web running?); stop it or pick --port",
                ) from exc
            raise
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None
        self._started = False

    @property
    def root(self) -> Path:
        return self.manager.root

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/"

    def start(self) -> "WebApp":
        if self._started:
            raise RuntimeError("web app is already running")
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.3},
            name="torrentkit-web",
            daemon=True,
        )
        self._thread.start()
        self._started = True
        return self

    def wait(self) -> None:
        if self._thread is None:
            return
        while self._thread.is_alive():
            time.sleep(0.4)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.manager.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


def serve_web(
    host: str = "127.0.0.1",
    port: int = 8080,
    directory: str | os.PathLike[str] = "downloads",
    *,
    open_browser: bool = False,
    announce_port: int = ANNOUNCE_PORT,
    max_active: int = DEFAULT_MAX_ACTIVE,
    allow_external_dirs: bool | None = None,
    logger=None,
) -> None:
    app = WebApp(
        directory,
        host=host,
        port=port,
        announce_port=announce_port,
        max_active=max_active,
        allow_external_dirs=allow_external_dirs,
        logger=logger,
    )
    app.start()
    print(f"torrentkit web UI: {app.url}")
    print(f"download dir:     {app.root.resolve()}")
    if host not in LOOPBACK_HOSTS:
        print(
            "warning: 页面没有登录认证，只有绑定在回环地址时才安全。",
            file=sys.stderr,
        )
    if open_browser:
        try:
            webbrowser.open(app.url)
        except Exception:
            pass
    try:
        app.wait()
    except KeyboardInterrupt:
        print()
    finally:
        app.stop()
