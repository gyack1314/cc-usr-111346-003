"""SocketTransport 回环测试（仅本机 socketpair，不接入任何真实系统）。"""
import socket
import threading
import unittest

from hl7_data.receiver import Ledger, ReceiverService, SocketTransport
from tests._hl7_messages import mllp, msh, pid, obr, obx


class SocketTransportTests(unittest.TestCase):
    def test_loopback_framing_and_ack(self):
        srv_sock, cli_sock = socket.socketpair()
        ledger = Ledger(':memory:')
        service = ReceiverService(ledger)
        data = mllp([msh('SOCK-1'), pid(), obr('O1'),
                     obx(1, 'NM', 'T^t', 'a', '1')])

        def client_job():
            # 任意分块：切断开始字节与多字节内容
            cli_sock.sendall(data[:15])
            cli_sock.sendall(data[15:])
            cli_sock.shutdown(socket.SHUT_WR)  # EOF：服务端 recv 返回空

        thread = threading.Thread(target=client_job)
        thread.start()
        try:
            transport = SocketTransport(srv_sock)
            results = service.drive_transport(transport)
            self.assertEqual(results[0].outcome, 'accepted')
            self.assertEqual(results[0].accept_ack, 'AA')
            service.flush_outbox(transport)
            echoed = cli_sock.recv(8192)
            self.assertTrue(echoed.startswith(bytes([0x0B])))
            self.assertTrue(echoed.endswith(bytes([0x1C, 0x0D])))
            self.assertIn(b'MSA|AA|SOCK-1', echoed)
        finally:
            thread.join()
            srv_sock.close()
            cli_sock.close()


if __name__ == '__main__':
    unittest.main()
