"""Command line interface for torrentkit."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .create import CreateError, create_torrent, write_torrent
from .download import Downloader, DownloadError, Progress, parse_peer_address, resolve_source
from .metainfo import Magnet, MetainfoError, Torrent
from .peers import make_peer_id
from .seeder import LocalSeeder, SeederError, seed_forever
from .storage import StorageError
from .tracker import TrackerError

LOG = logging.getLogger("torrentkit")

_SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(value: float) -> str:
    size = float(value)
    for unit in _SIZE_UNITS:
        if abs(size) < 1024.0 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PiB"


def parse_size(text: str) -> int:
    value = text.strip().lower()
    multipliers = {"k": 1024, "kb": 1024, "kib": 1024,
                   "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
                   "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3}
    suffix = ""
    while value and value[-1].isalpha():
        suffix = value[-1] + suffix
        value = value[:-1]
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"invalid size {text!r}")
    number = int(value)
    if not suffix:
        return number
    if suffix not in multipliers:
        raise argparse.ArgumentTypeError(f"unknown size suffix in {text!r}")
    return number * multipliers[suffix]


class _ProgressPrinter:
    def __init__(self) -> None:
        self.last = 0.0

    def _line(self, progress: Progress) -> str:
        return (
            f"{progress.fraction * 100:5.1f}%  "
            f"{human_size(progress.speed)}/s  "
            f"peers {progress.peers:<3d}  "
            f"pieces {progress.pieces_done}/{progress.pieces_total}"
        )

    def __call__(self, progress: Progress) -> None:
        now = time.monotonic()
        if sys.stderr.isatty():
            sys.stderr.write("\r" + self._line(progress))
            sys.stderr.flush()
        elif now - self.last >= 5.0 or progress.fraction >= 1.0:
            self.last = now
            print(self._line(progress), file=sys.stderr)

    def finish(self) -> None:
        if sys.stderr.isatty():
            sys.stderr.write("\n")
            sys.stderr.flush()


def _verify_printer(done: int, total: int) -> None:
    if not total:
        return
    if done == total or done % max(1, total // 20) == 0:
        sys.stderr.write(f"\rverifying {done * 100 // total:3d}% ({done}/{total} pieces)")
        sys.stderr.flush()
        if done == total:
            sys.stderr.write("\n")
            sys.stderr.flush()


# -- commands --------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    torrent = Torrent.from_file(args.torrent)
    if args.json:
        payload = {
            "name": torrent.name,
            "total_length": torrent.total_length,
            "piece_length": torrent.piece_length,
            "piece_count": torrent.piece_count,
            "info_hash": torrent.info_hash.hex() if torrent.has_v1_hashes else None,
            "info_hash_v2": torrent.info_hash_v2.hex() if torrent.info_hash_v2 else None,
            "v2": torrent.is_v2,
            "hybrid": torrent.is_hybrid,
            "private": torrent.private,
            "comment": torrent.comment,
            "trackers": torrent.trackers,
            "web_seeds": torrent.web_seeds,
            "magnet": torrent.magnet_uri(),
            "files": [
                {
                    "path": entry.display(torrent.name, torrent.is_single_file),
                    "length": entry.length,
                }
                for entry in torrent.files
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Name:        {torrent.name}")
    if torrent.has_v1_hashes:
        print(f"Info hash:   {torrent.info_hash.hex()}")
    else:
        print("Info hash:   n/a (BitTorrent v2 only; no v1 info hash)")
    if torrent.info_hash_v2:
        print(f"Info hash 2: {torrent.info_hash_v2.hex()}")
    print(f"Size:        {human_size(torrent.total_length)} ({torrent.total_length} bytes)")
    if torrent.piece_count:
        print(
            f"Pieces:      {torrent.piece_count} x {human_size(torrent.piece_length)}"
        )
    else:
        print("Pieces:      n/a (BitTorrent v2 only)")
    print(f"Private:     {'yes' if torrent.private else 'no'}")
    print(f"Trackers:    {len(torrent.trackers)}")
    for tracker in torrent.trackers:
        print(f"  - {tracker}")
    if torrent.web_seeds:
        print(f"Web seeds:   {len(torrent.web_seeds)}")
    if torrent.comment:
        print(f"Comment:     {torrent.comment}")
    print("Files:")
    for entry in torrent.files:
        print(f"  {entry.display(torrent.name, torrent.is_single_file)}  ({human_size(entry.length)})")
    return 0


def cmd_magnet(args: argparse.Namespace) -> int:
    torrent = Torrent.from_file(args.torrent)
    print(torrent.magnet_uri(include_web_seeds=not args.no_web_seeds))
    return 0


def cmd_parse_magnet(args: argparse.Namespace) -> int:
    magnet = Magnet.parse(args.uri)
    payload = {
        "info_hash": magnet.info_hash.hex() if magnet.info_hash else None,
        "info_hash_v2": magnet.info_hash_v2.hex() if magnet.info_hash_v2 else None,
        "name": magnet.dn,
        "size": magnet.xl,
        "trackers": magnet.trackers,
        "web_seeds": magnet.web_seeds,
        "xt": magnet.xt,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Name:       {magnet.display_name}")
        print(f"Info hash:  {payload['info_hash']}")
        if payload["info_hash_v2"]:
            print(f"Info hash 2:{payload['info_hash_v2']}")
        if magnet.xl:
            print(f"Size:       {human_size(magnet.xl)}")
        print(f"Trackers:   {len(magnet.trackers)}")
        for tracker in magnet.trackers:
            print(f"  - {tracker}")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    try:
        piece_length = parse_size(args.piece_length)
    except argparse.ArgumentTypeError as exc:
        raise CreateError(str(exc)) from exc
    metainfo = create_torrent(
        args.path,
        piece_length=piece_length,
        trackers=args.tracker or (),
        private=args.private,
        comment=args.comment,
    )
    torrent = Torrent(metainfo)
    output = Path(args.output) if args.output else Path(f"{torrent.name}.torrent")
    write_torrent(metainfo, output)
    print(f"Wrote {output} ({torrent.piece_count} pieces, {human_size(torrent.total_length)})")
    print(f"Info hash: {torrent.info_hash.hex()}")
    print(torrent.magnet_uri())
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    peers = [parse_peer_address(text) for text in args.peer or ()]
    peer_id = make_peer_id()
    torrent, magnet = resolve_source(
        args.source,
        extra_peers=peers,
        peer_id=peer_id,
        port=args.port,
        use_dht=not args.no_dht,
        logger=LOG,
    )

    print(f"Name:      {torrent.name}")
    print(f"Info hash: {torrent.info_hash.hex()}")
    print(f"Size:      {human_size(torrent.total_length)} in {torrent.piece_count} piece(s)")
    if torrent.trackers:
        print(f"Trackers:  {len(torrent.trackers)}")
    print(f"Save dir:  {Path(args.dir).resolve()}")
    if magnet is not None:
        print("Source:    magnet metadata exchange")

    downloader = Downloader(
        torrent,
        args.dir,
        port=args.port,
        peer_id=peer_id,
        extra_peers=peers,
        rarest_first=args.rarest_first,
        max_peers=args.max_peers,
        use_dht=not args.no_dht,
        logger=LOG,
    )
    printer = _ProgressPrinter()
    started = time.monotonic()
    try:
        ok = downloader.run(printer, verify_cb=_verify_printer)
    finally:
        printer.finish()
    elapsed = max(0.001, time.monotonic() - started)

    if ok:
        average = torrent.total_length / elapsed
        print(
            f"Complete: {human_size(torrent.total_length)} in {elapsed:.1f}s "
            f"({human_size(average)}/s) -> {Path(args.dir).resolve() / torrent.name}"
        )
        return 0
    done = downloader.state.downloaded if downloader.state else 0
    print(
        f"Incomplete: {human_size(done)} of {human_size(torrent.total_length)} downloaded",
        file=sys.stderr,
    )
    return 1


def cmd_serve(args: argparse.Namespace) -> int:
    torrent = Torrent.from_file(args.torrent)
    print(f"Name:      {torrent.name}")
    print(f"Info hash: {torrent.info_hash.hex()}")
    print(f"Pieces:    {torrent.piece_count}")
    print("Verifying local data...")
    seeder = LocalSeeder(
        torrent,
        args.dir,
        host=args.host,
        port=args.port,
        logger=LOG,
    )
    try:
        port = seeder.start()
        print(
            f"Seeding {len(seeder.have)}/{torrent.piece_count} pieces on "
            f"{args.host}:{port} (Ctrl+C to stop)"
        )
        print("Share this with a peer on your network:")
        print(f'  torrentkit download "{torrent.magnet_uri()}" --peer <your-ip>:{port}')
        seed_forever(seeder)
    finally:
        seeder.stop()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve_web

    allow_external = None
    if args.restrict_dirs:
        allow_external = False
    elif args.allow_external_dirs:
        allow_external = True
    serve_web(
        host=args.host,
        port=args.port,
        directory=args.dir,
        open_browser=args.open,
        max_active=args.max_active,
        allow_external_dirs=allow_external,
        logger=LOG,
    )
    return 0


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="torrentkit",
        description="Parse .torrent files, build magnet links and download over BitTorrent.",
    )
    parser.add_argument("--version", action="version", version=f"torrentkit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_verbose(target: argparse.ArgumentParser) -> None:
        target.add_argument("-v", "--verbose", action="store_true", help="verbose logging")

    info = sub.add_parser("info", help="show torrent details")
    info.add_argument("torrent")
    info.add_argument("--json", action="store_true", help="machine readable output")
    add_verbose(info)
    info.set_defaults(func=cmd_info)

    magnet = sub.add_parser("magnet", help="print the magnet URI for a torrent file")
    magnet.add_argument("torrent")
    magnet.add_argument("--no-web-seeds", action="store_true")
    add_verbose(magnet)
    magnet.set_defaults(func=cmd_magnet)

    parse_magnet = sub.add_parser("parse-magnet", help="inspect a magnet URI")
    parse_magnet.add_argument("uri")
    parse_magnet.add_argument("--json", action="store_true")
    add_verbose(parse_magnet)
    parse_magnet.set_defaults(func=cmd_parse_magnet)

    create = sub.add_parser("create", help="create a .torrent file from a file or directory")
    create.add_argument("path")
    create.add_argument("-o", "--output")
    create.add_argument("--tracker", action="append", default=[], help="tracker URL (repeatable)")
    create.add_argument("--piece-length", default="256KiB")
    create.add_argument("--private", action="store_true")
    create.add_argument("--comment")
    add_verbose(create)
    create.set_defaults(func=cmd_create)

    download = sub.add_parser("download", help="download a torrent file or magnet URI")
    download.add_argument("source", help="path to a .torrent file or a magnet: URI")
    download.add_argument("-d", "--dir", default=".", help="download directory")
    download.add_argument("--port", type=int, default=6881, help="listen port to announce")
    download.add_argument("--peer", action="append", default=[], help="extra peer HOST:PORT")
    download.add_argument("--max-peers", type=int, default=100)
    download.add_argument("--rarest-first", action="store_true")
    download.add_argument(
        "--no-dht",
        action="store_true",
        help="do not look up peers via DHT (always off for private torrents)",
    )
    add_verbose(download)
    download.set_defaults(func=cmd_download)

    serve = sub.add_parser("serve", help="seed a local, already verified copy")
    serve.add_argument("torrent")
    serve.add_argument("-d", "--dir", default=".", help="directory holding the torrent data")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=6881)
    add_verbose(serve)
    serve.set_defaults(func=cmd_serve)

    web = sub.add_parser("web", help="start the local web UI")
    web.add_argument("-d", "--dir", default="downloads", help="download root directory")
    web.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    web.add_argument("--port", type=int, default=8080)
    web.add_argument("--max-active", type=int, default=2, help="concurrent downloads (default: 2)")
    web.add_argument(
        "--allow-external-dirs",
        action="store_true",
        help="allow choosing download folders outside --dir (default on loopback)",
    )
    web.add_argument(
        "--restrict-dirs",
        action="store_true",
        help="only allow download folders inside --dir",
    )
    web.add_argument("--open", action="store_true", help="open the UI in a browser")
    add_verbose(web)
    web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(message)s",
    )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (
        DownloadError,
        MetainfoError,
        CreateError,
        SeederError,
        StorageError,
        TrackerError,
        ValueError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
