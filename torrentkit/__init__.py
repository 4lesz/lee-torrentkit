"""torrentkit: a small, dependency-free BitTorrent toolkit.

Public surface:

- :class:`torrentkit.metainfo.Torrent` parses ``.torrent`` files and exposes
  piece layout, file layout, trackers and v1/v2 info hashes.
- :meth:`Torrent.magnet_uri` turns metainfo into a magnet URI.
- :class:`torrentkit.metainfo.Magnet` parses magnet URIs.
- :class:`torrentkit.download.Downloader` downloads a torrent over the peer
  wire protocol.
- :class:`torrentkit.seeder.LocalSeeder` seeds a local, verified copy.
"""

from .bencode import BencodeError, decode, encode
from .metainfo import FileEntry, Magnet, MetainfoError, Torrent

__version__ = "0.1.0"
__author__ = "lee"

__all__ = [
    "BencodeError",
    "decode",
    "encode",
    "FileEntry",
    "Magnet",
    "MetainfoError",
    "Torrent",
    "__version__",
]
