"""Let Home Assistant's test plugin create an event loop on Windows.

The plugin blocks ``socket.socket`` and only exempts Unix sockets. The Windows
proactor loop opens its self-pipe with an IPv4 socketpair, so that guard has
to allow loopback sockets while still refusing other connections.
"""

from __future__ import annotations

import socket

import pytest_socket

_TRUE_SOCKET = socket.socket


def _disable_socket(allow_unix_socket: bool = False) -> None:
    """Block network sockets, but keep the Windows event-loop self-pipe working."""

    class GuardedSocket(_TRUE_SOCKET):
        def __new__(cls, family=-1, type=-1, proto=-1, fileno=None):
            if family in (socket.AF_INET, socket.AF_INET6):
                return _TRUE_SOCKET.__new__(cls, family, type, proto, fileno)
            try:
                unix = family == socket.AF_UNIX
            except AttributeError:
                unix = False
            if unix and allow_unix_socket:
                return _TRUE_SOCKET.__new__(cls, family, type, proto, fileno)
            raise pytest_socket.SocketBlockedError()

        def connect(self, address):  # type: ignore[no-untyped-def]
            host = address[0] if isinstance(address, tuple) else None
            if host in {"127.0.0.1", "::1", "localhost"}:
                return super().connect(address)
            raise pytest_socket.SocketConnectBlockedError(["127.0.0.1"], host)

    socket.socket = GuardedSocket  # type: ignore[misc]


pytest_socket.disable_socket = _disable_socket
