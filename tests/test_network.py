"""Exercise urllib itself; only DNS/socket destinations are redirected locally."""

import contextlib
import os
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from einkrelayer.capture import http, network

PUBLIC = "93.184.216.34"
IPV6 = "2606:4700:4700::1111"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Host")))
        if self.path in self.server.bodies:
            media, body = self.server.bodies[self.path]
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in self.server.redirects:
            self.send_response(302)
            self.send_header("Location", self.server.redirects[self.path])
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        stream = self.request.makefile("rb")
        first = stream.readline().decode().strip()
        headers = []
        while line := stream.readline().strip():
            headers.append(line.decode())
        self.server.requests.append((first, headers))
        if first.startswith("CONNECT "):
            if self.server.tls_port is None:
                self.request.sendall(b"HTTP/1.1 502 No test origin\r\nContent-Length: 0\r\n\r\n")
                return
            with self.server.real_connect(("127.0.0.1", self.server.tls_port), 3) as origin:
                self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                while True:
                    ready, _, _ = select.select([origin, self.request], [], [], 3)
                    if not ready:
                        return
                    for connection in ready:
                        try:
                            data = connection.recv(65536)
                        except ConnectionError:
                            return  # Expected when a client rejects the TLS certificate.
                        if not data:
                            return
                        (origin if connection is self.request else self.request).sendall(data)
        else:
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n\r\nok"
            )


@contextlib.contextmanager
def serving(server):
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.redirects = {}
        self.server.bodies = {}
        self.stack.enter_context(serving(self.server))
        self.port = self.server.server_port
        self.answers = {"example.test": [PUBLIC], "other.test": [PUBLIC]}
        self.connections = []
        self.real_connect = socket.create_connection
        real_dns = socket.getaddrinfo

        def dns(host, port, *args, **kwargs):
            if host in self.answers:
                return [
                    (
                        socket.AF_INET6 if ":" in ip else socket.AF_INET,
                        socket.SOCK_STREAM,
                        6,
                        "",
                        (ip, port),
                    )
                    for ip in self.answers[host]
                ]
            # Numeric addresses are resolved locally, never via public DNS.
            return real_dns(host, port, *args, **kwargs)

        def connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
            self.connections.append(address)
            host, port = address
            if host in {PUBLIC, IPV6}:
                host = "127.0.0.1"
            return self.real_connect((host, port), timeout, source_address, **kwargs)

        self.stack.enter_context(patch("socket.getaddrinfo", side_effect=dns))
        self.stack.enter_context(patch("socket.create_connection", side_effect=connect))

    def open(self, url):
        with network.urlopen(url, timeout=3) as response:
            return response.read()

    def proxy(self, tls_port=None):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Proxy)
        server.daemon_threads = True
        server.requests = []
        server.tls_port = tls_port
        server.real_connect = self.real_connect
        self.stack.enter_context(serving(server))
        url = f"http://127.0.0.1:{server.server_address[1]}"
        os.environ.update(http_proxy=url, https_proxy=url)
        return server

    def test_public_ipv4_ipv6_mixed_case_and_dns_pin(self):
        for host, ip in [("EXAMPLE.test", PUBLIC), ("example.test", IPV6)]:
            with self.subTest(host=host, ip=ip):
                self.answers["example.test"] = [ip]
                self.assertEqual(self.open(f"http://{host}:{self.port}/page"), b"ok")
                self.assertEqual(self.connections[-1], (ip, self.port))
                self.assertEqual(self.server.requests[-1], ("/page", f"example.test:{self.port}"))

    def test_rejects_private_mixed_mapped_translation_and_multicast_answers(self):
        for ip in [
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "::1",
            "fc00::1",
            "::ffff:8.8.8.8",
            "64:ff9b::808:808",
            "64:ff9b:1::1",
            "224.0.0.1",
        ]:
            with self.subTest(ip=ip):
                self.answers["example.test"] = [PUBLIC, ip]
                with self.assertRaisesRegex(ValueError, "non-public"):
                    self.open(f"http://example.test:{self.port}/")
        self.assertEqual(self.connections, [])

    def test_unicode_path_is_encoded_without_changing_query_semantics(self):
        self.assertEqual(self.open(f"http://example.test:{self.port}/茶?x=1&x=2"), b"ok")
        self.assertEqual(self.server.requests[-1][0], "/%E8%8C%B6?x=1&x=2")

    def test_redirect_revalidates_and_blocks_private_before_second_connection(self):
        self.server.redirects["/private"] = "http://127.0.0.1/secret"
        with self.assertRaisesRegex(ValueError, "non-public"):
            self.open(f"http://example.test:{self.port}/private")
        self.assertEqual(len(self.connections), 1)
        self.server.redirects["/public"] = f"http://other.test:{self.port}/done"
        self.assertEqual(self.open(f"http://example.test:{self.port}/public"), b"ok")
        self.assertEqual(self.server.requests[-1], ("/done", f"other.test:{self.port}"))
        self.server.redirects["/space"] = f"http://other.test:{self.port}/file name"
        self.assertEqual(self.open(f"http://example.test:{self.port}/space"), b"ok")
        self.assertEqual(self.server.requests[-1][0], "/file%20name")

    def test_proxy_absolute_target_connect_ipv6_and_no_proxy(self):
        proxy = self.proxy()
        for ip in [PUBLIC, IPV6]:
            with self.subTest(ip=ip):
                self.answers["example.test"] = [ip]
                self.assertEqual(self.open("http://EXAMPLE.test/path?x=1"), b"ok")
                authority = f"[{ip}]:80" if ":" in ip else f"{ip}:80"
                first, headers = proxy.requests[-1]
                self.assertEqual(first, f"GET http://{authority}/path?x=1 HTTP/1.1")
                self.assertIn("Host: example.test", headers)
                with self.assertRaises(URLError):
                    self.open("https://EXAMPLE.test/")
                authority = f"[{ip}]:443" if ":" in ip else f"{ip}:443"
                self.assertEqual(proxy.requests[-1][0], f"CONNECT {authority} HTTP/1.1")
        os.environ["no_proxy"] = "example.test"
        self.assertEqual(self.open(f"http://example.test:{self.port}/direct"), b"ok")
        self.assertEqual(self.connections[-1], (IPV6, self.port))

    def test_private_destination_rejected_even_with_trusted_proxy(self):
        proxy = self.proxy()
        with self.assertRaises(ValueError):
            self.open("http://127.0.0.1/secret")
        self.assertEqual(proxy.requests, [])

    def test_rejects_bad_urls_and_unsupported_proxy_transports(self):
        for url in [
            "https://example.test:0/",
            "https://example.test:/",
            "file:///etc/passwd",
            "https://u:p@example.test/",
            "https://example.test/\n",
            "http://example.test/\tx",
            "http://[fe80::1%25eth0]/",
            "http://example.test\\@127.0.0.1/",
        ]:
            # Whitespace surrounding a URL is allowed; embedded controls are not.
            if url.endswith("\n"):
                continue
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.open(url)
        os.environ["https_proxy"] = "https://127.0.0.1:443"
        with self.assertRaisesRegex(ValueError, "http:// proxy"):
            self.open("https://example.test/")

    def test_extensionless_document_downloads_use_checked_capture(self):
        from einkrelayer.capture.readers import read
        from tools.document_samples import create_samples

        with tempfile.TemporaryDirectory() as temporary:
            cases = {
                Path(case["file"]).name: case for case in create_samples(Path(temporary) / "inputs")
            }
            for name, media, path in [
                ("simple.pdf", "application/pdf", "/download/7"),
                (
                    "structured.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "/files/report?id=8",
                ),
            ]:
                with self.subTest(document=name):
                    case = cases[name]
                    self.server.bodies[path] = (media, Path(case["file"]).read_bytes())
                    url = f"http://example.test:{self.port}{path}"
                    document = read(url, cache=None)
                    self.assertEqual(document.source, url)
                    self.assertIn(case["required_text"][0], document.html)
                    self.assertIn((path, f"example.test:{self.port}"), self.server.requests)
                    self.assertIn((PUBLIC, self.port), self.connections)

    def test_tls_handshake_matches_the_standard_client(self):
        with patch("einkrelayer.capture.network.build_opener") as opener:
            network.urlopen("https://example.com/")
        context = opener.call_args.args[2]._context
        self.assertTrue(context.post_handshake_auth)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_dns_errors_are_retryable(self):
        with (
            patch("socket.getaddrinfo", side_effect=socket.gaierror("test DNS failure")),
            patch.object(http.time, "sleep") as sleep,
            self.assertRaises(URLError),
        ):
            http.fetch("https://example.test/", attempts=2)
        sleep.assert_called_once_with(1)

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture generation requires openssl")
    def test_real_tls_hostname_verification_direct_proxy_and_downgrade(self):
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        cert, key = directory / "cert.pem", directory / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=example.test",
                "-addext",
                "subjectAltName=DNS:example.test",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        names = []
        server_context.set_servername_callback(lambda sock, name, context: names.append(name))
        self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
        client_context = ssl.create_default_context(cafile=str(cert))
        self.stack.enter_context(
            patch.object(network.ssl, "create_default_context", return_value=client_context)
        )
        for proxy in [False, True]:
            with self.subTest(proxy=proxy):
                if proxy:
                    self.proxy(tls_port=self.port)
                self.assertEqual(self.open(f"https://EXAMPLE.test:{self.port}/tls"), b"ok")
                self.assertEqual(names[-1], "example.test")
                with self.assertRaises(URLError) as failure:
                    self.open(f"https://other.test:{self.port}/wrong-name")
                self.assertIsInstance(failure.exception.reason, ssl.SSLCertVerificationError)
                self.server.redirects["/down"] = f"http://example.test:{self.port}/insecure"
                count = len(self.connections)
                with self.assertRaisesRegex(ValueError, "HTTPS-to-HTTP"):
                    self.open(f"https://example.test:{self.port}/down")
                self.assertEqual(len(self.connections), count + 1)
        self.assertNotIn("/insecure", [p for p, _ in self.server.requests])
