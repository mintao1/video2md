import select
import socket
import threading

LISTEN = ("127.0.0.1", 18080)

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
    except Exception:
        pass
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
    client, _ = server.accept()
    threading.Thread(target=handle, args=(client,), daemon=True).start()
