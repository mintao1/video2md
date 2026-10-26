import os
import select
import socket
import sys
import threading

# Bind to loopback by default. Compose deployments opt into the Docker bridge
# address so containers can reach the relay without exposing it publicly.
LISTEN = (
    os.environ.get("V2PDF_OAUTH_RELAY_HOST", "127.0.0.1"),
    int(os.environ.get("V2PDF_OAUTH_RELAY_PORT", "18080")),
)
ALLOWED_IPS = {
    value.strip()
    for value in os.environ.get("V2PDF_OAUTH_RELAY_ALLOWED_IPS", "127.0.0.1").split(",")
    if value.strip()
}


def handle(client):
    upstream = None
    try:
        header = client.recv(4096).split(b"\r\n", 1)[0].decode("latin1")
        method, target, _ = header.split(" ", 2)
        if method != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, port = target.rsplit(":", 1)
        upstream = socket.create_connection((host, int(port)), 10)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        while True:
            readable, _, _ = select.select([client, upstream], [], [], 60)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is client else client).sendall(data)
    except (OSError, ValueError) as exc:
        print(f"OAuth relay connection failed: {type(exc).__name__}", file=sys.stderr)
    finally:
        for sock in (client, upstream):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(LISTEN)
server.listen(32)
while True:
    client, peer = server.accept()
    if peer[0] not in ALLOWED_IPS:
        client.close()
        print("OAuth relay rejected client", file=sys.stderr)
        continue
    threading.Thread(target=handle, args=(client,), daemon=True).start()
