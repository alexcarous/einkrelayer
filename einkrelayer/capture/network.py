"""Public-only HTTP(S), pinned DNS, verified TLS, and environment HTTP proxies.

Python 3.13's urllib handles redirects/proxy discovery; connection factories
pin origins explicitly, without changing the hostname used for TLS verification.
Proxy endpoints are trusted local configuration, never inferred from host case.
"""

import ipaddress
import re
import socket
import ssl
from http.client import HTTPConnection, HTTPSConnection
from urllib.error import URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from einkrelayer.capture.access import AccessRestricted


def normalized_url(url):
    url = url.strip()
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Expected an HTTP(S) URL without control characters")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "%" in parsed.hostname
        or parsed.netloc.endswith(":")
    ):
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")
    port = parsed.port
    if port is not None and not 0 < port < 65536:
        raise ValueError("Invalid HTTP(S) port")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return urlunsplit(
        (
            parsed.scheme,
            authority,
            quote(parsed.path, safe="/%:@!$&'()*+,;=-._~"),
            quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~[]"),
            "",
        )
    )


def public_address(host, port):
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise URLError(error) from error
    if not answers:
        raise URLError("DNS returned no addresses")
    for family, _, _, _, address in answers:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise ValueError("Unsupported destination address family")
        ip = ipaddress.ip_address(address[0])
        if (
            not ip.is_global
            or ip.is_multicast
            or (
                isinstance(ip, ipaddress.IPv6Address)
                and (
                    ip.ipv4_mapped is not None
                    or ip.sixtofour is not None
                    or ip.teredo is not None
                    or ip in ipaddress.ip_network("64:ff9b::/96")
                    or ip in ipaddress.ip_network("64:ff9b:1::/48")
                )
            )
        ):
            raise ValueError("Refusing a non-public destination")
    # All answers must be public. A numeric address cannot be rebound by DNS.
    return answers[0][4][0]


class _Redirects(HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = normalized_url(newurl.replace(" ", "%20"))
        if re.search(r"/(?:login|log-in|signin|sign-in)(?:/|$)", urlsplit(target).path, re.I):
            raise AccessRestricted("redirect to login")
        if urlsplit(req.full_url).scheme == "https" and urlsplit(target).scheme != "https":
            raise ValueError("Refusing an HTTPS-to-HTTP redirect")
        return super().redirect_request(req, fp, code, msg, headers, target)


class _Proxy(ProxyHandler):
    def proxy_open(self, req, proxy, type):
        if urlsplit(proxy).scheme not in {"", "http"}:
            raise ValueError("Capture requires an http:// proxy URL")
        return super().proxy_open(req, proxy, type)


def _open(handler, req, secure):
    original = urlsplit(normalized_url(req.full_url))
    port = original.port or (443 if original.scheme == "https" else 80)
    address = public_address(original.hostname, port)
    authority = f"[{address}]" if ":" in address else address
    authority += f":{port}"
    proxied = req.has_proxy() or bool(req._tunnel_host)
    if original.scheme == "http" and secure:
        # urllib does not provide separate TLS contexts for an HTTPS proxy and
        # an HTTPS origin. Fail visibly instead of accidentally skipping TLS.
        raise ValueError("Use an http:// proxy URL for capture")
    req.add_unredirected_header("Host", original.netloc)
    if req.has_proxy():
        req.selector = urlunsplit(
            (original.scheme, authority, original.path or "/", original.query, "")
        )

    class Connection(HTTPSConnection if secure else HTTPConnection):
        def __init__(self, host, **kwargs):
            super().__init__(host, **kwargs)
            if not proxied:
                self._create_connection = lambda target, timeout, source: socket.create_connection(
                    (address, port), timeout, source
                )

        def set_tunnel(self, host, port=None, headers=None):
            target = f"[{address}]" if ":" in address else address
            super().set_tunnel(target, original.port or 443, headers)

        def connect(self):
            if not secure:
                return super().connect()
            HTTPConnection.connect(self)
            self.sock = self._context.wrap_socket(self.sock, server_hostname=original.hostname)

    if secure:
        return handler.do_open(Connection, req, context=handler._context)
    return handler.do_open(Connection, req)


class _HTTP(HTTPHandler):
    def http_open(self, req):
        return _open(self, req, False)


class _HTTPS(HTTPSHandler):
    def https_open(self, req):
        return _open(self, req, True)


def urlopen(request, timeout=30):
    """Open a public destination with verified TLS and environment proxy support."""
    if isinstance(request, str):
        request = Request(normalized_url(request))
    else:
        normalized_url(request.full_url)
    # Use the standard Python HTTPS settings.
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    context.post_handshake_auth = True
    handlers = [_Proxy(), _HTTP(), _HTTPS(context=context), _Redirects()]
    return build_opener(*handlers).open(request, timeout=timeout)
