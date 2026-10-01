# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import argparse
import os
import sys
from seine.distributed.agent.daemon import WorkerAgent


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="seine worker agent daemon")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the worker agent daemon")
    run_parser.add_argument("--server", required=True, help="URL of seine-server (e.g. http://192.168.1.111:8000)")
    run_parser.add_argument(
        "--enrollment-token",
        default=os.environ.get("SEINE_ENROLLMENT_TOKEN"),
        help="Pre-shared enrollment token (default: $SEINE_ENROLLMENT_TOKEN)",
    )
    run_parser.add_argument(
        "--work-dir",
        default="/var/tmp/seine-agent",
        help="Persistent worker scratch directory (default: /var/tmp/seine-agent)",
    )
    run_parser.add_argument("--worker-id", help="Explicit worker ID (default: worker-<host>-<arch>)")
    run_parser.add_argument(
        "--ca-cert",
        default=os.environ.get("SEINE_CA_CERT"),
        help="CA bundle used to verify the server (default: $SEINE_CA_CERT)",
    )
    run_parser.add_argument(
        "--insecure",
        action="store_true",
        help="Allow plain http:// to a non-loopback server",
    )
    run_parser.add_argument(
        "--allow-root",
        action="store_true",
        help="Run even as root (builds then run as root too)",
    )

    args = parser.parse_args()

    if args.command == "run" or args.command is None:
        if not getattr(args, "server", None):
            parser.print_help()
            sys.exit(1)
        if os.geteuid() == 0 and not args.allow_root:
            parser.error("refusing to run as root: use an unprivileged user, or pass --allow-root")
        if not args.enrollment_token:
            parser.error("an enrollment token is required: --enrollment-token or SEINE_ENROLLMENT_TOKEN")
        try:
            agent = WorkerAgent(
                server_url=args.server,
                enrollment_token=args.enrollment_token,
                work_dir=args.work_dir,
                worker_id=args.worker_id,
                ca_cert=args.ca_cert,
                insecure=args.insecure,
            )
        except ValueError as e:
            parser.error(str(e))
        agent.start()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
