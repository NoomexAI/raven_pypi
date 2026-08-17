"""Run the local Raven HTTP/SSE server with ``python -m raven.server``."""

from __future__ import annotations

import argparse

import uvicorn

from .app import ServerSettings, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local RAVEN API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=None, help="Bearer token; generated when omitted")
    args = parser.parse_args()
    settings = ServerSettings(host=args.host, port=args.port, token=args.token or ServerSettings().token)
    print(f"RAVEN listening on http://{settings.host}:{settings.port}")
    print(f"RAVEN bearer token: {settings.token}")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()

