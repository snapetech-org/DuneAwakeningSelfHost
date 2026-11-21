"""Exercise the real shim with synthetic identities and a loopback upstream."""
import http.client
import http.server
import importlib.util
import json
import os
import pathlib
import sys
import threading
import urllib.parse


def main():
    received = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append((self.path, body))
            self.send_response(200)
            self.send_header('Content-Length', '14')
            self.end_headers()
            self.wfile.write(b'allow-upstream')

        def log_message(self, *args):
            pass

    upstream = http.server.HTTPServer(('127.0.0.1', 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    # All values are synthetic test data; no service secret is loaded or mounted.
    os.environ.update(WORLD_UNIQUE_NAME='fixture-world',
                      TEXT_ROUTER_AUTH_BASE='http://127.0.0.1:' + str(upstream.server_port),
                      DUNE_RMQ_MANAGEMENT_USER='fixture-manager',
                      DUNE_RMQ_MANAGEMENT_PASSWORD='synthetic-fixture-value')
    os.environ.pop('DUNE_RMQ_MANAGEMENT_PASSWORD_FILE', None)
    sys.path.insert(0, '/code/scripts')
    spec = importlib.util.spec_from_file_location('qualified_rmq_shim', pathlib.Path('/code/scripts/rmq_auth_shim.py'))
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    server = shim.AuthServer(('127.0.0.1', 0), shim.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, fields):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            body = urllib.parse.urlencode(fields).encode()
            connection.request('POST', path, body, {'Content-Type': 'application/x-www-form-urlencoded'})
            response = connection.getresponse()
            assert response.status == 200
            return response.read()
        finally:
            connection.close()

    try:
        assert request('/v0/auth/user', {'username': 'fixture-manager', 'password': 'synthetic-fixture-value'}) == b'allow administrator'
        assert request('/v0/auth/user', {'username': 'fixture-manager', 'password': 'incorrect-fixture-value'}) == b'deny'
        assert request('/v0/auth/vhost', {'username': 'fixture-manager'}) == b'allow'
        assert request('/v0/auth/user', {'username': 'sg.fixture-world.shard.game'}) == b'allow administrator'
        assert request('/v0/auth/topic', {'username': '0123456789ABCDEF'}) == b'allow'
        fields = {'username': 'unclassified-fixture', 'password': 'synthetic-upstream-value'}
        assert request('/v0/auth/user', fields) == b'allow-upstream'
        assert received == [('/v0/auth/user', urllib.parse.urlencode(fields).encode())]
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)
        assert request('/v0/auth/user', fields) == b'deny'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if upstream_thread.is_alive():
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=5)
    print(json.dumps({'checks': 7, 'passed': True, 'network': 'loopback-only',
                      'productionSecrets': False, 'upstreamFailureDenied': True}))


if __name__ == '__main__':
    main()
