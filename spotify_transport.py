"""HTTPS con plazo absoluto: DNS acotado y sockets sin trabajo HTTP abandonado."""
import http.client
import socket
import threading
from urllib.parse import urlsplit

_dns_lock = threading.Lock()
_dns_pending = {}


def resolve(host, port, budget):
    # Solo DNS va en otro hilo. Nunca se deja una petición/play en segundo plano.
    # Una resolución colgada se comparte: como máximo un hilo por host/puerto.
    key = (host, port)
    with _dns_lock:
        entry = _dns_pending.get(key)
        if entry is None or entry[0].is_set():
            entry = (threading.Event(), [])
            _dns_pending[key] = entry
            def work():
                try:
                    value = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                except Exception as exc:
                    value = exc
                entry[1].append(value)
                entry[0].set()
            threading.Thread(target=work, name="spotify-dns", daemon=True).start()
    while not entry[0].wait(budget.timeout(0.05)):
        pass
    budget.remaining()
    value = entry[1][0]
    if isinstance(value, Exception):
        raise value
    return value


class DeadlineHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, budget, request_timeout):
        super().__init__(host, port, timeout=budget.timeout(request_timeout))
        self.budget = budget
        self.request_timeout = request_timeout

    def limit(self):
        timeout = self.budget.timeout(self.request_timeout)
        if self.sock is not None:
            self.sock.settimeout(timeout)
        return timeout

    def connect(self):
        addresses = resolve(self.host, self.port, self.budget)
        last = OSError("sin direcciones para Spotify")
        for family, kind, proto, _, address in addresses:
            sock = socket.socket(family, kind, proto)
            self.sock = sock
            try:
                sock.settimeout(self.budget.timeout(self.request_timeout))
                sock.connect(address)
                sock.settimeout(self.budget.timeout(self.request_timeout))
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host,
                                                     do_handshake_on_connect=False)
                self.limit()
                self.sock.do_handshake()
                self.limit()
                return
            except Exception as exc:
                if self.sock is not None:
                    self.sock.close()
                sock.close()
                self.sock = None
                last = exc
                self.budget.remaining()
        raise last


def deadline_transport(method, url, headers, body, timeout, budget):
    target = urlsplit(url)
    if target.scheme != "https" or target.hostname not in {"api.spotify.com", "accounts.spotify.com"}:
        raise ValueError("destino HTTPS de Spotify no permitido")
    connection = DeadlineHTTPSConnection(target.hostname, target.port or 443, budget, timeout)
    finished = threading.Event()
    connected_socket = [None]
    response = None
    def guard():
        while not finished.wait(0.02):
            try:
                budget.remaining()
            except Exception:
                # getresponse puede separar el socket si el servidor pide
                # Connection: close; el cuerpo aún lo usa mediante makefile.
                sock = connection.sock or connected_socket[0]
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return
    # Cierra el socket incluso si el servidor gotea cabeceras o bytes. No ejecuta HTTP.
    threading.Thread(target=guard, name="spotify-deadline", daemon=True).start()
    try:
        connection.connect()
        connected_socket[0] = connection.sock
        connection.limit()
        path = target.path + ("?" + target.query if target.query else "")
        connection.request(method, path, body=body, headers=headers)
        connection.limit()
        response = connection.getresponse()
        chunks = []
        while True:
            connection.limit()
            chunk = response.read1(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return response.status, {k.lower(): v for k, v in response.getheaders()}, b"".join(chunks)
    finally:
        finished.set()
        if response is not None:
            response.close()
        connection.close()
