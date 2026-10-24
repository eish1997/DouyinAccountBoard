"""Desktop entry point. The parent Electron process owns this server's lifetime."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app
from http.server import ThreadingHTTPServer

def main():
    app.initialize()
    app.backup.start(app.DB)
    server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    print(f'BOARD_URL=http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
