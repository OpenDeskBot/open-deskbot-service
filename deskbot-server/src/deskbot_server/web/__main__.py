"""python -m deskbot_server.web"""
from __future__ import annotations

import os

from deskbot_server.env import load_dotenv
from deskbot_server.web.app import app


def main() -> None:
    load_dotenv()
    host = (os.environ.get("DESKBOT_WEB_HOST") or "0.0.0.0").strip()
    port = int(os.environ.get("DESKBOT_WEB_PORT") or "5050")
    app.run(host=host, port=port, debug=True)


if __name__ == "__main__":
    main()
