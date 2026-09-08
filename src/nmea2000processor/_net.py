"""Small networking helper shared by geocode.py/weather.py/marine.py -- the only thing these
three modules need beyond plain urllib.request.urlopen()."""

from __future__ import annotations

import queue
import socket
import threading
import urllib.request
from typing import Any


def _call_with_timeout(func, args, timeout: float):
    """Runs func(*args) on a throwaway daemon thread and waits at most timeout seconds for it.

    socket.getaddrinfo() has no timeout parameter of its own -- it blocks on the OS resolver for
    however long that takes, found in practice to matter for real: on a flaky roaming mobile
    connection, a single getaddrinfo() call sat blocked for minutes with nothing timing it out,
    freezing the whole sync (no crash, stable memory, just silently stuck) well past the
    urlopen(timeout=...) callers below actually intended to wait.

    A plain threading.Thread (not e.g. a shared ThreadPoolExecutor) specifically -- a shared pool
    would let one truly-never-returns lookup permanently occupy its only worker, deadlocking
    every later lookup for the rest of the process's life. A fresh daemon thread per call cannot
    do that: if it never returns, it just leaks harmlessly (collected when the process exits)
    instead of blocking anything else."""
    result: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            result.put(("ok", func(*args)))
        except Exception as exc:  # re-raised on the caller's side below, whatever it is
            result.put(("error", exc))

    threading.Thread(target=_run, daemon=True).start()
    try:
        status, value = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"{func.__name__}{args} timed out after {timeout}s") from None
    if status == "error":
        raise value
    return value


def urlopen_ipv4_first(request: urllib.request.Request, timeout: float) -> Any:
    """Same as urllib.request.urlopen(request, timeout=timeout), but resolves the target host to
    its IPv4 address(es) first when any exist, instead of leaving address-family selection to
    socket.getaddrinfo()'s own default order (often IPv6-first).

    Found in practice on a real Android device, real and reproducible: while on a roaming mobile
    network, every single request to a dual-stack host (both nominatim.openstreetmap.org and
    overpass-api.de have both A and AAAA records) failed with "[Errno 101] Network is
    unreachable", even though a browser on the same phone and the same network worked fine at the
    very same moment. `adb shell dumpsys connectivity` showed why: the phone's active default
    network's own LinkProperties carried IPv4 routes only, no IPv6 route at all (that specific
    roaming network's IPv6/NAT64 path was apparently not working, though the exact carrier-side
    reason doesn't matter here) -- a browser's own Happy Eyeballs (RFC 8305) logic tries both
    address families and quietly uses whichever succeeds, but plain urllib does not: it hands the
    address list from getaddrinfo() to socket.create_connection(), which does try every address
    in order, but only after the earlier (here: IPv6) ones already failed outright. Trying IPv4
    first sidesteps needing to know why a given network's IPv6 route is missing or broken.

    Falls back to the plain getaddrinfo() order if the IPv4-only lookup itself fails or times out
    (e.g. a genuinely IPv6-only host, or the DNS query for A records specifically doesn't succeed)
    -- this must never make a host that only has AAAA records look unreachable. Every
    getaddrinfo() call is itself bounded by _call_with_timeout (see its own doc comment) -- DNS
    resolution has no timeout of its own otherwise, found in practice to matter."""
    original_getaddrinfo = socket.getaddrinfo

    def _ipv4_preferred(host, port, family=0, type=0, proto=0, flags=0):
        if family in (0, socket.AF_UNSPEC):
            try:
                ipv4_results = _call_with_timeout(
                    original_getaddrinfo, (host, port, socket.AF_INET, type, proto, flags), timeout
                )
            except Exception:
                ipv4_results = None
            if ipv4_results:
                return ipv4_results
        return _call_with_timeout(original_getaddrinfo, (host, port, family, type, proto, flags), timeout)

    # Not thread-safe against *concurrent* network calls elsewhere in the same process -- fine
    # here, since geocode.py/weather.py/marine.py are only ever called sequentially, one lookup
    # at a time, from a single background thread (see android_entry.py/cli.py).
    socket.getaddrinfo = _ipv4_preferred
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    finally:
        socket.getaddrinfo = original_getaddrinfo
