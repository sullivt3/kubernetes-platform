"""One-destination CONNECT proxy prevents Git alternate URLs bypassing pinned DNS."""

from __future__ import annotations

import select
import socket
import socketserver
import threading
import time


class PinnedTunnel:
    def __init__(self, host: str, port: int, address: str, deadline: float):
        self.host, self.port, self.address, self.deadline = host, port, address, deadline
        self.transferred = 0
        self.lock = threading.Lock()
        tunnel = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    self.connection.settimeout(10)
                    line = self.rfile.readline(4096).decode("ascii").strip().split()
                    expected = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                    if len(line) != 3 or line[0] != "CONNECT" or line[1].lower() != expected.lower():
                        self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                        return
                    for _ in range(64):
                        header = self.rfile.readline(4096)
                        if header == b"\r\n":
                            break
                        if len(header) >= 4096 or not header:
                            return
                    else:
                        return
                    with socket.create_connection((address, port), timeout=5) as upstream:
                        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        self.wfile.flush()
                        peers = [self.connection, upstream]
                        while time.monotonic() < tunnel.deadline:
                            ready, _, _ = select.select(peers, [], [], 1)
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                with tunnel.lock:
                                    tunnel.transferred += len(data)
                                    if tunnel.transferred > 128 * 1024 * 1024:
                                        return
                                (upstream if source is self.connection else self.connection).sendall(data)
                except (OSError, ValueError, UnicodeError):
                    return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.deadline = 0
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
