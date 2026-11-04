"""Socket helpers shared by the listening parts (web UI, seeder, DHT)."""

from __future__ import annotations

import errno
import socket

# Windows reports "address in use" as WSAEADDRINUSE, and WSAEACCES when another
# socket holds the port with SO_EXCLUSIVEADDRUSE.
_IN_USE_WINERRORS = (10048, 10013)


def configure_reuse(sock: socket.socket) -> None:
    """Allow fast rebinds after restart without letting two servers share a port.

    On POSIX ``SO_REUSEADDR`` only skips TIME_WAIT. On Windows it lets a second
    process bind a port that is already listening, so requests may silently go
    to a stale server; use ``SO_EXCLUSIVEADDRUSE`` there instead.
    """
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def is_address_in_use(exc: OSError) -> bool:
    return exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in _IN_USE_WINERRORS
