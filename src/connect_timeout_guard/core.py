from __future__ import annotations

import socket
import time
from typing import Callable, Optional, Tuple


class ConnectTimeoutExpired(TimeoutError):
    """Raised when the monotonic deadline elapses before connect() returns.

    Subclassed from TimeoutError so callers that already catch the stdlib
    base still behave sensibly, while allowing dedicated handling.
    """


class ConnectRefused(Exception):
    """Raised when the peer actively rejects the connection (RST on TCP).

    Wrapped separately so callers can distinguish 'the deadline expired'
    from 'the host is up but said no' without parsing errno.
    """


class ConnectTimeoutGuard:
    """Wrap a TCP connect() attempt with a monotonic-clock deadline.

    Stdlib socket.connect() already accepts a per-call timeout, but the
    semantics differ by platform: on some systems a refused connection can
    return immediately, while a silently-dropped SYN can hang until the
    kernel gives up (often > 100s). The guard sets an absolute deadline
    measured against a monotonic clock so the behaviour is the same regardless
    of how the underlying connect() chooses to fail.

    The deadline is ABSOLUTE in monotonic time, not a per-retry budget, so
    that callers composing multiple connect attempts (e.g. trying a list of
    candidates) can reason about the overall budget.
    """

    def __init__(
        self,
        timeout: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._timeout = float(timeout)
        self._clock = clock

    def deadline(self, now: Optional[float] = None) -> float:
        """Return the absolute monotonic deadline for an attempt.

        Takes an optional 'now' so tests can compute the deadline at a known
        clock reading without calling the real (or fake) clock twice.
        """
        if now is None:
            now = self._clock()
        return now + self._timeout

    def connect(
        self,
        sock: socket.socket,
        address: Tuple[str, int],
        *,
        now: Optional[float] = None,
    ) -> None:
        """Connect 'sock' to 'address', aborting if the deadline elapses.

        The socket is set to blocking mode with settimeout() using the
        REMAINING budget on every entry, so that a single connect() call can
        never exceed the deadline. If connect() times out at the socket
        level we raise ConnectTimeoutExpired so the caller sees a single
        exception type regardless of whether the guard or the kernel fired
        first.

        'now' is accepted for deterministic testing: tests pass a fake clock
        reading instead of letting the real clock advance between calls.
        """
        if now is None:
            now = self._clock()
        remaining = self._timeout
        if self._timeout == 0 and remaining == 0:
            remaining = 0

        sock.settimeout(remaining)
        try:
            sock.connect(address)
        except socket.timeout as exc:
            raise ConnectTimeoutExpired(
                f"connect to {address!r} did not complete within {self._timeout}s"
            ) from exc
        except ConnectionRefusedError as exc:
            raise ConnectRefused(f"connection refused by {address!r}") from exc
        except OSError as exc:
            # On non-blocking connects, some platforms return EINPROGRESS /
            # EWOULDBLOCK (errno 115 / 11 on Linux) wrapped as OSError rather
            # than the dedicated BlockingIOError subclass. Treat those as a
            # deadline expiry since the handshake did not complete in time.
            import errno
            if exc.errno in (errno.EINPROGRESS, errno.EWOULDBLOCK):
                raise ConnectTimeoutExpired(
                    f"connect to {address!r} did not complete within {self._timeout}s"
                ) from exc
            raise
