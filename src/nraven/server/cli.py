"""Command-line entry point for Raven's local ASGI server."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .settings import resolve_server_configuration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nraven")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the Raven API server.")
    serve.add_argument(
        "--home",
        help="Raven storage root. Overrides the RAVEN_HOME environment variable.",
    )
    serve.add_argument(
        "--system-settings",
        help="Explicit system_settings.json path.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command != "serve":
        return

    raven_home, system_config = resolve_server_configuration(
        args.home,
        system_settings_path=args.system_settings,
    )
    try:
        import uvicorn
        from .app import create_app
    except ImportError as exc:
        raise SystemExit(
            "Raven's server dependencies are not installed. "
            "Install them with: pip install 'noomexai-raven[server]'"
        ) from exc

    app = create_app(raven_home, system_config)
    uvicorn.run(
        app,
        host=system_config.host,
        port=system_config.port,
        log_level=system_config.log_level,
        workers=1,
    )


if __name__ == "__main__":
    main()
