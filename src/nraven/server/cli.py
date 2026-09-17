"""Command-line entry point for Raven's local ASGI server."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from collections.abc import Sequence

from ..core.errors import ErrorCode, RavenError
from .ownership import ServerOwnershipLock
from .settings import resolve_server_configuration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nraven")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the Raven API server.")
    serve.add_argument(
        "--home",
        required=True,
        help="Required Raven storage root.",
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

    auth_dir = Path(raven_home) / "temp" / "server_auth"
    ownership = ServerOwnershipLock(auth_dir / "server.lock")
    try:
        ownership.acquire()
    except RavenError as error:
        if error.code == ErrorCode.SERVER_ALREADY_RUNNING:
            raise SystemExit(error.message) from error
        raise

    try:
        app = create_app(raven_home, system_config)
        token_path = auth_dir / "bearer_token"
        launch_token = app.state.bearer_token
        temporary_path = token_path.with_name(f".{token_path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(launch_token + "\n")
            os.replace(temporary_path, token_path)
            try:
                os.chmod(token_path, 0o600)
            except OSError:
                pass
            uvicorn.run(
                app,
                host=system_config.host,
                port=system_config.port,
                log_level=system_config.log_level,
                workers=1,
            )
        finally:
            temporary_path.unlink(missing_ok=True)
            try:
                if token_path.read_text(encoding="utf-8").strip() == launch_token:
                    token_path.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        ownership.release()


if __name__ == "__main__":
    main()
