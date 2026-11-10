"""Private-network fixture and real SOCKS5 TCP/UDP probe (stdlib only)."""
import http.server
import ipaddress
import json
import socket
import ssl
import struct
import sys
import threading


def read_exact(sock, count):
    data = b''
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise EOFError('SOCKS peer closed early')
        data += chunk
    return data


def dns_answer(query, address):
    if len(query) < 12 or struct.unpack('!H', query[4:6])[0] != 1:
        return None
    end = 12
    try:
        while query[end]:
            end += query[end] + 1
        end += 5
        qtype = struct.unpack('!H', query[end - 4:end - 2])[0]
        if end > len(query):
            return None
    except (IndexError, struct.error):
        return None
    answer = b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x01\x00\x04' + socket.inet_aton(address) if qtype == 1 else b''
    return query[:2] + struct.pack('!HHHHH', 0x8180, 1, bool(answer), 0, 0) + query[12:end] + answer


def serve(address, cert, key):
    events = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(events if self.path == '/events' else {'peer': self.client_address[0], 'path': self.path}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    def udp(port):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(('0.0.0.0', port))
            while True:
                data, peer = sock.recvfrom(65535)
                if port == 53:
                    events.append({'peer': peer[0], 'query': data.hex()})
                    reply = dns_answer(data, address)
                else:
                    reply = json.dumps({'peer': peer[0], 'data': data.decode()}).encode()
                if reply:
                    sock.sendto(reply, peer)

    for port in (53, 9999):
        threading.Thread(target=udp, args=(port,), daemon=True).start()
    cover = http.server.ThreadingHTTPServer(('0.0.0.0', 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(cert, key)
    cover.socket = context.wrap_socket(cover.socket, server_side=True)
    threading.Thread(target=cover.serve_forever, daemon=True).start()
    http.server.ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()


def socks_address(host):
    """Encode a SOCKS wire address without resolving domain names locally."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        encoded = host.encode('idna')
        return b'\x03' + bytes([len(encoded)]) + encoded
    return bytes([1 if address.version == 4 else 4]) + address.packed


def socks_request(port, command, host, target_port):
    sock = socket.create_connection(('127.0.0.1', port), timeout=4)
    sock.sendall(b'\x05\x01\x00')
    assert read_exact(sock, 2) == b'\x05\x00'
    sock.sendall(b'\x05' + bytes([command, 0]) + socks_address(host) + struct.pack('!H', target_port))
    header = read_exact(sock, 4)
    assert header[:2] == b'\x05\x00', 'SOCKS request rejected'
    atyp = header[3]
    if atyp == 1:
        bound = socket.inet_ntoa(read_exact(sock, 4))
    elif atyp == 3:
        bound = read_exact(sock, read_exact(sock, 1)[0]).decode()
    elif atyp == 4:
        bound = socket.inet_ntop(socket.AF_INET6, read_exact(sock, 16))
    else:
        raise ValueError('Invalid SOCKS address')
    return sock, bound, struct.unpack('!H', read_exact(sock, 2))[0]


def probe(port, protocol, host='target.invalid', path='/egress'):
    if protocol == 'tcp':
        sock, _, _ = socks_request(port, 1, host, 8080)
        with sock:
            sock.sendall(('GET ' + path + ' HTTP/1.0\r\nHost: target.invalid\r\n\r\n').encode())
            data = b''
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                data += part
        return json.loads(data.split(b'\r\n\r\n', 1)[1])
    control, bound, udp_port = socks_request(port, 3, '0.0.0.0', 0)
    with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(4)
        udp.sendto(b'\x00\x00\x00' + socks_address(host) + struct.pack('!H', 9999) + b'japan-udp', (bound, udp_port))
        data, _ = udp.recvfrom(65535)
        assert data[:3] == b'\x00\x00\x00'
        offset = {1: 10, 4: 22}.get(data[3])
        if data[3] == 3:
            offset = 7 + data[4]
        return json.loads(data[offset:])


if __name__ == '__main__':
    if sys.argv[1] == 'serve':
        serve(*sys.argv[2:])
    elif sys.argv[1] == 'probe':
        print(json.dumps(probe(int(sys.argv[2]), sys.argv[3], *sys.argv[4:])))
    elif sys.argv[1] == 'isolate':
        # Both literal-address connectivity and DNS must fail from the frontend.
        try:
            socket.getaddrinfo('target.invalid', 8080)
        except socket.gaierror:
            pass
        else:
            raise AssertionError('Frontend resolved private target')
        try:
            socket.create_connection((sys.argv[2], 8080), timeout=2)
        except OSError:
            pass
        else:
            raise AssertionError('Frontend reached private target')
        print('frontend DNS and direct TCP isolated')
