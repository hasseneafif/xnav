#!/usr/bin/env python3
"""xnav init [path] — analyze a project and open the codebase navigator."""
from __future__ import annotations
import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Load .env.local then .env from the xnav directory (where this file lives).
# Must happen before any os.getenv() calls.
_HERE = Path(__file__).parent
try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env.local", override=True)
    load_dotenv(_HERE / ".env", override=False)
except ImportError:
    pass


def _open_browser(port: int, delay: float = 3.0) -> None:
    time.sleep(delay)
    webbrowser.open(f"http://localhost:{port}")


def cmd_init(path: str, port: int, no_browser: bool) -> None:
    project_path = Path(path).resolve()
    if not project_path.exists():
        print(f"  [xnav] Error: path does not exist: {project_path}", file=sys.stderr)
        sys.exit(1)

    print(f"  [xnav] Analyzing {project_path}")
    print(f"  [xnav] Serving at http://localhost:{port}")

    os.environ["XNAV_PROJECT_PATH"] = str(project_path)

    if not no_browser:
        threading.Thread(target=_open_browser, args=(port,), daemon=True).start()

    try:
        import uvicorn
    except ImportError:
        print("  [xnav] Error: uvicorn not installed. Run: pip install uvicorn[standard]", file=sys.stderr)
        sys.exit(1)

    uvicorn.run(
        "api.main:app",
        host=os.getenv("XNAV_HOST", "0.0.0.0"),
        port=port,
        log_level="info",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xnav",
        description="Xnav — AI-powered codebase navigator",
    )
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="Analyze a project and open the navigator")
    init.add_argument("path", nargs="?", default=".", help="Path to project root (default: current directory)")
    init.add_argument("--port", type=int, default=int(os.getenv("XNAV_PORT", "8000")), help="Port (default: 8000)")
    init.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args.path, args.port, args.no_browser)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
