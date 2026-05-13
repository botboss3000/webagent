"""webAgent launcher — pre-opens port with SO_REUSEADDR for zombie-port resilience."""
import socket
import uvicorn
from uvicorn.config import Config
from uvicorn.server import Server

HOST = "0.0.0.0"
PORT = 8080


def main():
    # Pre-open socket with SO_REUSEADDR
    # This works around orphaned LISTENING entries (process dead, port still bound)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, PORT))
        sock.listen(256)
        print(f"[webAgent] Bound port {PORT} with SO_REUSEADDR")
    except OSError as e:
        print(f"[webAgent] Pre-open failed ({e}), falling back to direct bind")
        sock.close()
        sock = None

    config = Config(
        app="app.main:app",
        host=HOST,
        port=PORT,
        ws="wsproto",
    )
    server = Server(config=config)

    # Run startup manually to print our own "listening" message
    # (uvicorn skips the "Uvicorn running on" line when sockets are passed)
    import sys
    print(f"[webAgent] Server running on http://{HOST}:{PORT}", flush=True)
    print(f"[webAgent] UI: http://localhost:{PORT}/index.html", flush=True)
    print(f"[webAgent] Swagger: http://localhost:{PORT}/docs", flush=True)
    print(f"[webAgent] Press Ctrl+C in the webAgent.bat window to stop.", flush=True)
    print(flush=True)

    server.run(sockets=[sock] if sock else None)


if __name__ == "__main__":
    main()
