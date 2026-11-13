"""Start the read-only Streamlit server bound exclusively to localhost."""

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from betting_scanner import dashboard


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local read-only arbitrage dashboard")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--headless", action="store_true", help="Do not open a browser")
    args = parser.parse_args(argv)
    if not args.project_root.is_dir() or not 1024 <= args.port <= 65535:
        parser.error("Choose an existing project root and port between 1024 and 65535")
    environment = dict(os.environ)
    environment["BETTING_DASHBOARD_ROOT"] = str(args.project_root.resolve())
    environment["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    try:
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(Path(dashboard.__file__).with_name("app.py").resolve()),
                "--server.address",
                "127.0.0.1",
                "--server.port",
                str(args.port),
                "--server.headless",
                str(args.headless).lower(),
                "--browser.gatherUsageStats",
                "false",
            ],
            env=environment,
        )
    except KeyboardInterrupt:
        return 130
