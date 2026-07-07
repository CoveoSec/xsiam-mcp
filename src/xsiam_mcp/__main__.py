"""CLI entry point.

Usage:
    xsiam-mcp            # run the MCP server over stdio (default)
    xsiam-mcp --list     # print the generated tool catalog and exit (no creds)
    xsiam-mcp --count    # print the number of generated tools and exit
"""

from __future__ import annotations

import sys

from .server import XsiamMCP, main as run_server


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("--list", "-l"):
        app = XsiamMCP()
        print(app._list_operations(None))
        return
    if argv and argv[0] in ("--count", "-c"):
        app = XsiamMCP()
        print(f"{len(app.operations)} generated XSIAM tools (+3 composite).")
        return
    if argv and argv[0] in ("--help", "-h"):
        print(__doc__)
        return
    run_server()


if __name__ == "__main__":
    main()
