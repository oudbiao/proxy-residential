"""Wire-format regressions for the isolated end-to-end fixture."""
import socket
import struct
import unittest
import threading
from e2e_target import dns_answer, read_exact, probe


class FixtureTests(unittest.TestCase):
    def test_dns_response_preserves_query_and_returns_requested_ipv4(self):
        query = struct.pack('!HHHHHH', 123, 0x100, 1, 0, 0, 0) + b'\x06target\x07invalid\x00\x00\x01\x00\x01'
        reply = dns_answer(query, '192.0.2.7')
        self.assertEqual(struct.unpack('!HHHHHH', reply[:12]), (123, 0x8180, 1, 1, 0, 0))
        self.assertEqual(reply[12:len(query)], query[12:])
        self.assertEqual(reply[-4:], socket.inet_aton('192.0.2.7'))
        self.assertIsNone(dns_answer(b'bad', '192.0.2.7'))

    def test_read_exact_rejects_early_eof(self):
        left, right = socket.socketpair()
        try:
            right.sendall(b'ab')
            right.shutdown(socket.SHUT_WR)
            with self.assertRaises(EOFError):
                read_exact(left, 3)
        finally:
            left.close()
            right.close()

    def test_udp_probe_emits_domain_ipv4_and_ipv6_wire_addresses(self):
        # Observe real SOCKS control and UDP sockets; numeric text encoded as
        # ATYP=domain must fail the literal-IP assertions.
        for host, expected in (
                ('target.invalid', b'\x03\x0etarget.invalid'),
                ('192.0.2.7', b'\x01\xc0\x00\x02\x07'),
                ('2001:db8::7', b'\x04' + socket.inet_pton(socket.AF_INET6, '2001:db8::7'))):
            with self.subTest(host=host), socket.socket() as listener, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                listener.bind(('127.0.0.1', 0))
                listener.listen()
                listener.settimeout(3)
                udp.bind(('127.0.0.1', 0))
                udp.settimeout(3)
                captured = []
                def relay():
                    control, _ = listener.accept()
                    with control:
                        read_exact(control, 3)
                        control.sendall(b'\x05\x00')
                        header = read_exact(control, 4)
                        size = {1: 4, 4: 16}.get(header[3])
                        if header[3] == 3:
                            size = read_exact(control, 1)[0]
                        read_exact(control, size + 2)
                        control.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01' + struct.pack('!H', udp.getsockname()[1]))
                        data, peer = udp.recvfrom(65535)
                        captured.append(data)
                        udp.sendto(b'\x00\x00\x00\x01\x7f\x00\x00\x01\x27\x0f{"ok":true}', peer)
                worker = threading.Thread(target=relay, daemon=True)
                worker.start()
                self.assertEqual(probe(listener.getsockname()[1], 'udp', host), {'ok': True})
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(captured, [b'\x00\x00\x00' + expected + b'\x27\x0fjapan-udp'])
