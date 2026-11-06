#!/usr/bin/env python3
"""Run the read-only build check on the host when Admin has no Bash/Steam path."""
import json
import os
import pathlib
import socket
import subprocess
import sys


def main():
    socket_path = os.environ.get("DUNE_RESTART_API_SOCKET")
    if not socket_path:
        script = pathlib.Path(__file__).with_name("check-steam-update.sh")
        return subprocess.call([str(script), *sys.argv[1:]])
    payload = json.dumps({
        "target": "all", "services": [], "action": "restart",
        "phase": "check-current-build", "dryRun": False,
        "fastDynamicStart": False, "jobId": "build-preflight",
    }).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(95)
        sock.connect(socket_path)
        sock.sendall(b"POST /v1/restart HTTP/1.1\r\nHost: dune-restart-api\r\n"
                     b"Connection: close\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        response = bytearray()
        while chunk := sock.recv(65536):
            response.extend(chunk)
            if len(response) > 512 * 1024:
                raise ValueError("host build-check response exceeds its size limit")
    headers, separator, body = response.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("host build check returned an invalid HTTP response")
    status = int(headers.split(b" ", 2)[1])
    result = json.loads(body)
    print(result.get("output") or result.get("error") or "host build check returned no evidence")
    return 0 if status == 200 and result.get("ok") is True and result.get("exitCode") == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"current-build verification failed: {exc}", file=sys.stderr)
        sys.exit(2)
