"""Find a working local proxy, so nobody has to hunt for a port number.

A candidate counts as usable only when a TLS handshake with api.telegram.org completes
through the tunnel, which proves the proxy is up and its upstream actually reaches Telegram.
"""

from __future__ import annotations

import socket
import ssl
import struct

HOST = "api.telegram.org"
PORT = 443

# Ports used by the clients people actually run in China, roughly by popularity.
CANDIDATE_PORTS = (
    7890, 7897, 7899, 7891,
    1080, 1081, 10808, 10809,
    2080, 2081, 8889, 1087,
    6152, 6153, 8118, 20171, 20172, 2018,
)

TCP_TIMEOUT = 0.25
PROBE_TIMEOUT = 1.5


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError("connection closed")
        data += chunk
    return data


def _socks5_tunnel(sock: socket.socket) -> None:
    sock.sendall(b"\x05\x01\x00")
    if _recv_exact(sock, 2) != b"\x05\x00":
        raise OSError("socks5 handshake rejected")
    request = b"\x05\x01\x00\x03" + bytes((len(HOST),)) + HOST.encode() + struct.pack(">H", PORT)
    sock.sendall(request)
    head = _recv_exact(sock, 4)
    if head[1] != 0:
        raise OSError("socks5 connect refused")
    address_type = head[3]
    if address_type == 1:
        _recv_exact(sock, 4)
    elif address_type == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0])
    elif address_type == 4:
        _recv_exact(sock, 16)
    else:
        raise OSError("unsupported address type")
    _recv_exact(sock, 2)


def _http_tunnel(sock: socket.socket) -> None:
    request = f"CONNECT {HOST}:{PORT} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n\r\n".encode()
    sock.sendall(request)
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(1024)
        if not chunk:
            raise OSError("connection closed")
        response += chunk
        if len(response) > 4096:
            raise OSError("proxy response too long")
    if b" 200 " not in response.split(b"\r\n")[0]:
        raise OSError("proxy refused CONNECT")


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return True
    except OSError:
        return False


def _tunnel_works(host: str, port: int, scheme: str) -> bool:
    context = ssl.create_default_context()
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=PROBE_TIMEOUT)
        sock.settimeout(PROBE_TIMEOUT)
        if scheme == "socks5":
            _socks5_tunnel(sock)
        else:
            _http_tunnel(sock)
        context.wrap_socket(sock, server_hostname=HOST).close()
        return True
    except (OSError, ssl.SSLError, ValueError):
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def direct_works(timeout: float = 1.2) -> bool:
    """True when Telegram is reachable without any proxy."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            context.wrap_socket(sock, server_hostname=HOST).close()
        return True
    except (OSError, ssl.SSLError):
        return False


def detect(host: str = "127.0.0.1") -> list[str]:
    """Working proxy URLs, most likely first."""
    found: list[str] = []
    for port in CANDIDATE_PORTS:
        if not _port_open(host, port):
            continue
        # A mixed port answers both; socks5 keeps hostname resolution on the proxy side.
        for scheme in ("socks5", "http"):
            if _tunnel_works(host, port, scheme):
                found.append(f"{scheme}://{host}:{port}")
                break
    return found


def describe(urls: list[str]) -> str:
    return "、".join(urls) if urls else "未检测到"
