"""Where Minecraft's host link is, and the token that opens it.

The mod writes a fresh token to <PASSTHROUGH_WIN_DIR>\\passthrough.token every time it starts, and refuses
any handshake that doesn't present it (or that carries a browser's Origin header). Loopback alone is not a
boundary: every local process can reach the port, and a web page the player has open can open a WebSocket to
127.0.0.1 from any origin with no CORS check in the way - and {"t":"cmd"} runs server commands as an operator.

    from link import url
    async with websockets.connect(url()) as ws: ...
"""
import os
from pathlib import Path

PORT = int(os.environ.get("PASSTHROUGH_PORT", 25599))
WIN_DIR = Path(os.environ.get("PASSTHROUGH_WIN_DIR", r"C:\dev\passthrough"))
TOKEN_FILE = WIN_DIR / "passthrough.token"


def token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SystemExit(f"no host link token in {TOKEN_FILE} ({e}); start Minecraft with the passthrough mod "
                         "first, or point PASSTHROUGH_WIN_DIR at the folder it writes to")


def url() -> str:
    """ws://127.0.0.1:<port>/?token=... - read fresh each time, since the mod makes a new one per run."""
    return f"ws://127.0.0.1:{PORT}/?token={token()}"
