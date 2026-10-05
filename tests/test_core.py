import errno
import socket
import threading
import unittest
from unittest.mock import MagicMock

from connect_timeout_guard import (
    ConnectTimeoutGuard,
    ConnectTimeoutExpired,
    ConnectRefused,
)


class FakeClock:
    """Deterministic monotonic clock for tests.

    Exposes advance() so a test can step the clock forward by a known amount
    instead of sleeping. This keeps the suite fast and machine-independent.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += float(dt)


class TestConstruction(unittest.TestCase):
    def test_negative_timeout_rejected(self):
        with self.assertRaises(ValueError):
            ConnectTimeoutGuard(-0.1)

    def test_zero_timeout_allowed(self):
        guard = ConnectTimeoutGuard(0.0)
        self.assertEqual(guard._timeout, 0.0)

    def test_default_clock_is_monotonic(self):
        import time

        guard = ConnectTimeoutGuard(1.0)
        self.assertIs(guard._clock, time.monotonic)


class TestDeadline(unittest.TestCase):
    def test_deadline_uses_clock(self):
        clock = FakeClock(start=500.0)
        guard = ConnectTimeoutGuard(2.5, clock=clock)
        self.assertEqual(guard.deadline(), 502.5)

    def test_deadline_accepts_explicit_now(self):
        clock = FakeClock(start=10.0)
        guard = ConnectTimeoutGuard(3.0, clock=clock)
        self.assertEqual(guard.deadline(now=20.0), 23.0)


class TestConnectHappyPath(unittest.TestCase):
    def test_successful_connect_uses_remaining_as_settimeout(self):
        clock = FakeClock(start=0.0)
        guard = ConnectTimeoutGuard(5.0, clock=clock)

        sock = MagicMock(spec=socket.socket)
        guard.connect(sock, ("127.0.0.1", 1), now=0.0)

        sock.settimeout.assert_called_once_with(5.0)
        sock.connect.assert_called_once_with(("127.0.0.1", 1))

    def test_zero_timeout_sets_zero_settimeout(self):
        guard = ConnectTimeoutGuard(0.0, clock=FakeClock())
        sock = MagicMock(spec=socket.socket)
        guard.connect(sock, ("127.0.0.1", 1), now=0.0)
        sock.settimeout.assert_called_once_with(0)


class TestConnectTimeout(unittest.TestCase):
    def test_socket_timeout_is_wrapped(self):
        guard = ConnectTimeoutGuard(0.01, clock=FakeClock())
        sock = MagicMock(spec=socket.socket)
        sock.connect.side_effect = socket.timeout("timed out")

        with self.assertRaises(ConnectTimeoutExpired):
            guard.connect(sock, ("127.0.0.1", 1), now=0.0)

    def test_socket_timeout_chains_original(self):
        guard = ConnectTimeoutGuard(0.01, clock=FakeClock())
        sock = MagicMock(spec=socket.socket)
        original = socket.timeout("timed out")
        sock.connect.side_effect = original

        try:
            guard.connect(sock, ("127.0.0.1", 1), now=0.0)
        except ConnectTimeoutExpired as exc:
            self.assertIs(exc.__cause__, original)
        else:
            self.fail("expected ConnectTimeoutExpired")

    def test_einprogress_is_treated_as_timeout(self):
        guard = ConnectTimeoutGuard(0.01, clock=FakeClock())
        sock = MagicMock(spec=socket.socket)
        sock.connect.side_effect = OSError(errno.EINPROGRESS, "in progress")

        with self.assertRaises(ConnectTimeoutExpired):
            guard.connect(sock, ("127.0.0.1", 1), now=0.0)

    def test_ewouldblock_is_treated_as_timeout(self):
        guard = ConnectTimeoutGuard(0.01, clock=FakeClock())
        sock = MagicMock(spec=socket.socket)
        sock.connect.side_effect = OSError(errno.EWOULDBLOCK, "would block")

        with self.assertRaises(ConnectTimeoutExpired):
            guard.connect(sock, ("127.0.0.1", 1), now=0.0)

    def test_other_oserror_is_reraised(self):
        guard = ConnectTimeoutGuard(0.01, clock=FakeClock())
        sock = MagicMock(spec=socket.socket)
        sock.connect.side_effect = OSError(errno.EACCES, "permission denied")

        with self.assertRaises(OSError) as ctx:
            guard.connect(sock, ("127.0.0.1", 1), now=0.0)
        self.assertEqual(ctx.exception.errno, errno.EACCES)


class TestConnectRefused(unittest.TestCase):
    def test_connection_refused_is_wrapped(self):
        guard = ConnectTimeoutGuard(1.0, clock=FakeClock())
        sock = MagicMock(spec=socket.socket)
        sock.connect.side_effect = ConnectionRefusedError("refused")

        with self.assertRaises(ConnectRefused):
            guard.connect(sock, ("127.0.0.1", 1), now=0.0)

    def test_refused_chains_original(self):
        guard = ConnectTimeoutGuard(1.0, clock=FakeClock())
        sock = MagicMock(spec=socket.socket)
        original = ConnectionRefusedError("refused")
        sock.connect.side_effect = original

        try:
            guard.connect(sock, ("127.0.0.1", 1), now=0.0)
        except ConnectRefused as exc:
            self.assertIs(exc.__cause__, original)
        else:
            self.fail("expected ConnectRefused")


class TestRealSocket(unittest.TestCase):
    """Exercise the guard against a real listening socket on localhost.

    Uses a loopback listener that is guaranteed to exist in the container
    without external network access. The connect either succeeds or raises a
    refused error depending on whether the listener is up; both are valid
    deterministic outcomes for this test, which only checks that the guard
    does not crash or hang.
    """

    def test_connect_to_open_listener_succeeds(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        try:
            guard = ConnectTimeoutGuard(2.0)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                guard.connect(client, (host, port))
            finally:
                client.close()
        finally:
            server.close()

    def test_connect_to_closed_port_refused(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()
        server.close()

        guard = ConnectTimeoutGuard(2.0)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises((ConnectRefused, ConnectTimeoutExpired)):
                guard.connect(client, (host, port))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
