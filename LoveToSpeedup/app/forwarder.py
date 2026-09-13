import socket
import threading


class TcpForwarder:
    def __init__(self, listen_port, target_host, target_port):
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.server = None
        self.running = False

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", self.listen_port))
        self.server.listen(50)
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _pipe(self, src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _handle(self, client):
        upstream = None
        try:
            if not self.target_port:
                client.close()
                return
            upstream = socket.create_connection((self.target_host, self.target_port), timeout=5)
        except OSError:
            client.close()
            return
        t1 = threading.Thread(target=self._pipe, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=self._pipe, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        client.close()
        try:
            upstream.close()
        except OSError:
            pass

    def stop(self):
        self.running = False
        if self.server:
            try:
                self.server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.server.close()
            except OSError:
                pass
