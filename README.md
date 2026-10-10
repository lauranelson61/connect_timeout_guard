# Connect Timeout Guard

Wraps a `socket.connect()` attempt with an absolute monotonic-clock deadline, aborting if the TCP handshake does not complete in time.

```python
import socket
from connect_timeout_guard import ConnectTimeoutGuard, ConnectTimeoutExpired, ConnectRefused

guard = ConnectTimeoutGuard(timeout=2.0)
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    guard.connect(sock, ("127.0.0.1", 54321))
except ConnectRefused as exc:
    print("peer said no:", exc)
except ConnectTimeoutExpired as exc:
    print("handshake did not finish in 2s:", exc)
```

## Why this exists

`socket.connect()` already accepts a per-call timeout, but the behaviour differs by platform: a silently-dropped SYN can hang until the kernel's own backoff gives up (often well over a minute), and a refused connection may surface as a plain `OSError` rather than the dedicated `ConnectionRefusedError`. This guard imposes an absolute deadline measured against a monotonic clock, so callers composing multiple connect attempts can reason about a single overall budget. The trade-off: the deadline is absolute per guard instance, not per retry. If you want to try several addresses against one budget, advance the guard's clock between attempts or instantiate a new guard for each.

## Awkward edges

On non-blocking sockets, some kernels return `EINPROGRESS` or `EWOULDBLOCK` wrapped as a plain `OSError` rather than the `BlockingIOError` subclass. The guard treats both errnos as a deadline expiry, because from the caller's perspective the handshake did not finish. If you pass a non-blocking socket and your platform uses a different errno for the same situation, that case will surface as a raw `OSError`. `ConnectRefused` is raised only for the stdlib `ConnectionRefusedError`; other connection failures propagate as their original exception type.

The constructor accepts a `clock` callable so tests can pass a fake monotonic clock instead of relying on wall time.

## Performance

The window keeps a bounded buffer, so `push` is constant time and memory does not
grow with the length of the stream. `peak` and `trough` are linear in the window
size, which is the trade that keeps `push` cheap.

