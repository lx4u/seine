# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# CLI entry point and argument parsing for the interactive TUI.

from seine import settings
from seine.tui.app import SeineApp


# Entry point for the TUI. 'argv' may contain --interaction-socket
# (or --interaction-socket=PATH), --remote[=URL] (with --insecure and
# --ca-cert[=PATH] for the connection) followed by zero or
# more spec files; flags are stripped before the rest are treated as specs.
def run(argv=None):
    # Manual parsing, consistent with the rest of the CLI's getopt use.
    spec_files: list[str] = []
    socket_path: str | None = None
    remote_target: str | None = None
    connect_remote: bool = False
    remote_insecure: bool | None = None
    remote_ca_cert: str | None = None
    if argv:
        args = list(argv)
        idx = 0
        while idx < len(args):
            arg = args[idx]
            idx += 1
            if arg.startswith("--interaction-socket"):
                if arg == "--interaction-socket":
                    if idx >= len(args):
                        raise ValueError("--interaction-socket requires a path")
                    socket_path = args[idx]
                    idx += 1
                else:
                    _, _, path = arg.partition("=")
                    if not path:
                        raise ValueError("--interaction-socket requires a path")
                    socket_path = path
                continue
            if arg == "--insecure":
                remote_insecure = True
                continue
            if arg.startswith("--ca-cert"):
                if arg == "--ca-cert":
                    if idx >= len(args):
                        raise ValueError("--ca-cert requires a path")
                    path = args[idx]
                    idx += 1
                else:
                    _, _, path = arg.partition("=")
                if not path:
                    raise ValueError("--ca-cert requires a path")
                remote_ca_cert = settings.check_ca_cert(path)
                continue
            if arg.startswith("--remote"):
                connect_remote = True
                if arg == "--remote":
                    if idx < len(args) and not args[idx].startswith("-") and not args[idx].endswith((".yaml", ".yml")):
                        remote_target = args[idx]
                        idx += 1
                else:
                    _, _, r_url = arg.partition("=")
                    if r_url:
                        remote_target = r_url
                continue
            spec_files.append(arg)
    SeineApp(files=spec_files or None, interaction_socket=socket_path,
             remote=remote_target, connect_remote=connect_remote,
             remote_insecure=remote_insecure, remote_ca_cert=remote_ca_cert).run()
