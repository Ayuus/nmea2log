"""The local-network primitives shared by the W2K-2 search and the iOS app (the Android app has its own copy of the same
three ranges in HotspotDetector.kt, tested by HotspotDetectorTest.kt with the same boundary cases)."""

import socket

from nmea2log.subnet import is_private_ipv4, outbound_ipv4, subnet_prefix_of


def test_10_dot_x_dot_x_dot_x_is_private_regardless_of_the_second_octet():
    assert is_private_ipv4("10.0.0.1")
    assert is_private_ipv4("10.255.255.255")
    assert is_private_ipv4("10.190.25.4")


def test_172_dot_16_through_172_dot_31_is_private():
    assert is_private_ipv4("172.16.0.1")
    assert is_private_ipv4("172.31.255.255")
    assert is_private_ipv4("172.20.5.5")


def test_172_dot_15_and_172_dot_32_are_just_outside_the_private_range():
    assert not is_private_ipv4("172.15.255.255")
    assert not is_private_ipv4("172.32.0.0")


def test_192_dot_168_dot_x_dot_x_is_private():
    assert is_private_ipv4("192.168.0.1")
    assert is_private_ipv4("192.168.255.255")


def test_192_dot_167_and_192_dot_169_are_not_private():
    assert not is_private_ipv4("192.167.0.1")
    assert not is_private_ipv4("192.169.0.1")


def test_public_link_local_and_loopback_addresses_are_not_private():
    assert not is_private_ipv4("8.8.8.8")
    assert not is_private_ipv4("1.1.1.1")
    assert not is_private_ipv4("169.254.10.10")
    assert not is_private_ipv4("127.0.0.1")


def test_malformed_addresses_are_not_private():
    assert not is_private_ipv4("not-an-ip")
    assert not is_private_ipv4("10.0.0")
    assert not is_private_ipv4("")


def test_subnet_prefix_of_a_private_address_is_its_first_three_octets_with_a_trailing_dot():
    assert subnet_prefix_of("10.190.25.4") == "10.190.25."
    assert subnet_prefix_of("192.168.1.42") == "192.168.1."


def test_subnet_prefix_of_a_public_address_is_none():
    assert subnet_prefix_of("8.8.8.8") is None


class _FakeSocket:
    def __init__(self, *args, fail=False, address="192.168.0.62"):
        self._fail = fail
        self._address = address

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def connect(self, target):
        if self._fail:
            raise OSError("network is unreachable")

    def getsockname(self):
        return (self._address, 54321)


def test_outbound_ipv4_is_the_address_the_os_would_send_from(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _FakeSocket())

    assert outbound_ipv4() == "192.168.0.62"


def test_outbound_ipv4_is_none_without_a_route(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _FakeSocket(fail=True))

    assert outbound_ipv4() is None
