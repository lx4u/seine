# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Server CLI entry point."""

import argparse
import logging
import sys

import uvicorn

from seine.distributed.server.api import create_app
from seine.distributed.server.settings import Settings, SettingsError


def configure_logging(level: str = "info") -> None:
    """Send the seine loggers to stderr (the journal) unless logging is already set up."""
    logger = logging.getLogger("seine")
    logger.setLevel(level.upper())
    if logger.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)


def main():
    parser = argparse.ArgumentParser(prog="seine-server", description="seine distributed server")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the API and WebSocket server")
    run_parser.add_argument("--config", default=None, help="Config file (default: /etc/seine/server.yaml)")
    run_parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    run_parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8000)")
    run_parser.add_argument("--enrollment-token", default=None, help="Worker enrollment pre-shared token (required)")
    run_parser.add_argument("--db-path", default=None, help="SQLite database path")
    run_parser.add_argument("--tls-cert", default=None, help="TLS certificate file (needs --tls-key)")
    run_parser.add_argument("--tls-key", default=None, help="TLS private key file (needs --tls-cert)")
    run_parser.add_argument("--s3-endpoint", default=None, help="S3 endpoint URL of the shared storage")
    run_parser.add_argument("--s3-region", default=None, help="S3 region name (default: garage)")
    run_parser.add_argument("--storage-type", default=None, choices=["s3", "artifactory"], help="Shared storage backend (default: s3)")
    run_parser.add_argument("--artifactory-endpoint", default=None, help="Artifactory base URL of the shared storage")
    run_parser.add_argument("--stale-after", type=float, default=None, help="Seconds of silence before a worker is stale")
    run_parser.add_argument("--native-grace", type=float, default=None, help="Seconds a job waits for a better-scoring idle worker (default: 30, 0 disables)")
    run_parser.add_argument("--job-lost-grace", type=float, default=None, help="Seconds a claimed job may go unreported by its worker before it is requeued (default: 90)")

    run_parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"], help="Log level of the seine loggers (default: info)")

    admin_parser = subparsers.add_parser("admin", help="Administrative operations on local database")
    admin_parser.add_argument("--db-path", default=None, help="SQLite database path")
    from seine.distributed.server.admin import handle_admin_command, setup_admin_subparsers
    setup_admin_subparsers(admin_parser)

    args = parser.parse_args()

    if args.command == "run" or args.command is None:
        flags = ("host", "port", "enrollment_token", "db_path", "tls_cert", "tls_key", "stale_after", "native_grace", "job_lost_grace", "s3_endpoint", "s3_region", "storage_type", "artifactory_endpoint")
        try:
            settings = Settings.load(
                config_path=getattr(args, "config", None),
                overrides={name: getattr(args, name, None) for name in flags},
            )
            settings.validate()
        except SettingsError as e:
            print(f"seine-server: {e}", file=sys.stderr)
            sys.exit(2)
        if settings.exposed_without_tls:
            print(
                f"seine-server: warning: listening on {settings.host} without TLS, "
                "tokens travel in clear text",
                file=sys.stderr,
            )
        configure_logging(getattr(args, "log_level", "info"))
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            ssl_certfile=settings.tls_cert,
            ssl_keyfile=settings.tls_key,
        )
    elif args.command == "admin":
        sys.exit(handle_admin_command(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
