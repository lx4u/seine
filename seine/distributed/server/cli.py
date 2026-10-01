# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Server CLI entry point."""

import argparse
import os
import sys
import uvicorn


def main():
    parser = argparse.ArgumentParser(prog="seine-server", description="seine distributed server")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the API and WebSocket server")
    run_parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    run_parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    run_parser.add_argument("--enrollment-token", default=None, help="Worker enrollment pre-shared token")
    run_parser.add_argument("--db-path", default=None, help="SQLite database path")
    run_parser.add_argument("--reload", action="store_true", help="Enable auto-reload")

    args = parser.parse_args()

    if args.command == "run" or args.command is None:
        host = getattr(args, "host", "0.0.0.0")
        port = getattr(args, "port", 8000)
        reload = getattr(args, "reload", False)
        if getattr(args, "enrollment_token", None):
            os.environ["SEINE_ENROLLMENT_TOKEN"] = args.enrollment_token
        if getattr(args, "db_path", None):
            os.environ["SEINE_DB_PATH"] = args.db_path

        uvicorn.run("seine.distributed.server.api:app", host=host, port=port, reload=reload)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
