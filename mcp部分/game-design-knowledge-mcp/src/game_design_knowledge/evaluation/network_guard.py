"""Fail an evaluation run when anything tries to reach the network."""

from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import socket
from typing import Iterator
from unittest import mock


class NetworkAccessDuringEvaluation(RuntimeError):
    """Raised when evaluation code attempts a network operation."""


@contextmanager
def block_network(violations: list[str]) -> Iterator[None]:
    """Record and refuse every attempt to leave the machine.

    Loopback is allowed because asyncio's Windows event loop builds its own
    self-pipe from a loopback socket pair; blocking that would break the MCP
    client rather than the network policy.
    """

    def refuse(operation: str, host_index: int = 0):
        def guard(*args, **kwargs):
            if len(args) > host_index and _is_loopback(args[host_index]):
                return _original(operation)(*args, **kwargs)
            detail = f"network access attempted through {operation}: {args[:1]}"
            violations.append(detail)
            raise NetworkAccessDuringEvaluation(detail)

        return guard

    originals = {
        "socket.connect": socket.socket.connect,
        "socket.create_connection": socket.create_connection,
        "socket.getaddrinfo": socket.getaddrinfo,
    }

    def _original(operation: str):
        return originals[operation]

    with mock.patch.object(
        socket.socket, "connect", refuse("socket.connect", host_index=1)
    ), mock.patch.object(
        socket, "create_connection", refuse("socket.create_connection")
    ), mock.patch.object(
        socket, "getaddrinfo", refuse("socket.getaddrinfo")
    ):
        yield


def _is_loopback(address: object) -> bool:
    host: object = address
    if isinstance(address, tuple) and address:
        host = address[0]
    if not isinstance(host, str):
        return False
    if host in {"", "localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
