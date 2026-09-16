from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import uvicorn

from backend import create_app, get_settings

SETTINGS = get_settings()
ROOT = Path(__file__).resolve().parent
HOST = SETTINGS.host
PORT = SETTINGS.port
APPDATA_ROOT = ROOT / ".azurjuus"
WORKSPACE_FILE = SETTINGS.workspace_state_path


class UvicornServerAdapter:
    def __init__(self, host: str = HOST, port: int = PORT, runtime_context: dict | None = None):
        self._runtime_context = runtime_context if runtime_context is not None else {}
        self.app = create_app(runtime_context=self._runtime_context)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((host, port))
        self._socket.listen(2048)
        self.server_address = self._socket.getsockname()
        self.app.state.runs.endpoint = f"http://{self.server_address[0]}:{self.server_address[1]}/api/internal/tool"
        config = uvicorn.Config(
            self.app,
            host=self.server_address[0],
            port=self.server_address[1],
            log_level="warning",
            lifespan="on",
            # 0.52.4's auto-selected SansIO transport can send a second close
            # during a QWebEngine page unload and abort server shutdown.
            ws="websockets",
        )
        self._server = uvicorn.Server(config)
        self._loop = None
        self._shutdown_future = None

    def serve_forever(self) -> None:
        async def run():
            self._loop = asyncio.get_running_loop()
            await self._server.serve(sockets=[self._socket])
        asyncio.run(run())

    def shutdown(self) -> None:
        # Cancel model/tool waits before Uvicorn waits for HTTP handlers to drain.
        if self._loop and self._loop.is_running() and self._shutdown_future is None:
            self._shutdown_future = asyncio.run_coroutine_threadsafe(self.app.state.runs.close(), self._loop)
        self._server.should_exit = True

    def server_close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


def create_server(host: str = HOST, port: int = PORT, runtime_context: dict | None = None) -> UvicornServerAdapter:
    return UvicornServerAdapter(host=host, port=port, runtime_context=runtime_context)


def serve(server: UvicornServerAdapter) -> None:
    host, port = server.server_address
    print(f"AzurJuus local server running at http://{host}:{port}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAzurJuus server stopped.")
    finally:
        server.server_close()


def main() -> None:
    serve(create_server())


if __name__ == "__main__":
    main()
