import socket
import threading
import unittest

from tools.l4s.tcp_forward import handle


class TcpForwardTest(unittest.TestCase):
    def test_upstream_half_close_preserves_client_reply(self):
        """A server FIN must not discard the browser's final ACK/close data."""
        with socket.create_server(("127.0.0.1", 0)) as target:
            with socket.create_server(("127.0.0.1", 0)) as proxy:
                client = socket.create_connection(proxy.getsockname(), timeout=3)
                accepted, _ = proxy.accept()
            worker = threading.Thread(
                target=handle, args=(accepted, target.getsockname(), None, None),
                daemon=True,
            )
            worker.start()
            upstream, _ = target.accept()
            upstream.settimeout(3)
            with upstream, client:
                upstream.sendall(b"final audit")
                upstream.shutdown(socket.SHUT_WR)
                received = bytearray()
                while data := client.recv(1024):
                    received.extend(data)
                self.assertEqual(received, b"final audit")
                # This is legal after receiving FIN: our sending half is open.
                client.sendall(b"browser close acknowledgement")
                client.shutdown(socket.SHUT_WR)
                reply = bytearray()
                while data := upstream.recv(1024):
                    reply.extend(data)
                self.assertEqual(reply, b"browser close acknowledgement")
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
