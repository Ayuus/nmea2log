"""The small pieces of local-network knowledge the W2K-2 search and the apps share: which addresses are private, the
subnet an address is on, and the address this machine would use to reach the internet. Plain standard library, no
platform code: Android finds its hotspot's subnet from its own interface list (HotspotDetector.kt, which needs the
platform), iOS and the desktop ask the OS for the address of the outbound route."""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional

_PRIVATE_RANGES = [ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def is_private_ipv4(ip: str) -> bool:
    """10.0.0.0/8, 172.16.0.0/12 and 192.168.0.0/16 (the three ranges of Android's HotspotDetector.isPrivateIpv4()).
    False for anything malformed, not only for what is not private; also for link-local (169.254.x.x) and loopback."""
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(address in network for network in _PRIVATE_RANGES)


def subnet_prefix_of(ip: str) -> Optional[str]:
    """The first three octets and a trailing dot (e.g. "10.190.25."), or None if ``ip`` is not a private IPv4 address."""
    if not is_private_ipv4(ip):
        return None
    return ".".join(ip.split(".")[:3]) + "."


def outbound_ipv4() -> Optional[str]:
    """The address the OS would send traffic to the internet from, or None if it cannot tell (no route). A UDP
    "connect" sends nothing, it only makes the kernel choose a route -- right whenever the OS prefers wifi over
    cellular, which is the normal case."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None
