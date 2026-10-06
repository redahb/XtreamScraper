"""Refusing requests to local and private networks (server-side request forgery guard).

URLs that come from remote data (e.g. artwork URLs returned by a metadata source) must not
make the application fetch from the user's own machine or LAN. A host is allowed only when
every address it resolves to is a public unicast address.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Iterable, Optional

#: ``host -> addresses``; injectable so tests never touch real DNS.
Resolver = Callable[[str], Iterable[str]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Shared address space (carrier-grade NAT, RFC 6598).
CGNAT = ipaddress.ip_network("100.64.0.0/10")


def system_resolver(host: str) -> list[str]:
    """Every address the system resolver returns for ``host`` (raises ``OSError`` on failure)."""
    # sockaddr[0] is the address text for AF_INET/AF_INET6 (typeshed also allows int).
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def literal_address(host: str) -> Optional[IPAddress]:
    """``host`` as an IP address when it is an IP literal, else ``None``."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_public_address(address: str) -> bool:
    """False for private, loopback, link-local, reserved, multicast, unspecified and CGNAT
    addresses (IPv4-mapped/compatible IPv6 forms included) and for anything unparseable.

    Contract:
        Silences:
            - ``ValueError`` from parsing: any non-address text (a host name included) is
              simply "not public". It fails closed by design.
    """
    text = str(address).split("%", 1)[0]  # drop an IPv6 zone id (fe80::1%eth0)
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip.teredo:
            embedded = ip.teredo[1]
        if embedded is None and int(ip) >> 32 == 0 and int(ip) > 1:  # deprecated IPv4-compatible ::a.b.c.d
            embedded = ipaddress.IPv4Address(int(ip))
        if embedded is not None and not is_public_address(str(embedded)):
            return False
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
            or ip.is_unspecified or not ip.is_global:
        return False
    return not (isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT)


def public_host_problem(host: Optional[str], resolver: Resolver) -> Optional[str]:
    """``None`` when ``host`` only resolves to public addresses, else why it is refused.

    Resolution failures raise ``OSError`` (the caller decides whether that is transient).

    Contract:
        Preconditions:
            - ``resolver`` is only called for a host that is not an IP literal; whatever it
              raises propagates.

        Raises:
            OSError: When ``resolver`` fails (``socket.gaierror`` from the system resolver) or
                returns no address (explicit check).

        Silences:
            - The answer covers every address only at resolution time; a later connection may
              resolve again (DNS rebinding is the caller's concern).
    """
    if not host:
        return "the URL has no host"
    literal = literal_address(host)
    addresses = [str(literal)] if literal is not None else list(resolver(host))
    if not addresses:
        raise OSError(f"no addresses for {host}")
    if any(not is_public_address(address) for address in addresses):
        return "the URL points to a private or local network address"
    return None
