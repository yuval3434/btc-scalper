"""
push_trades.py -- Commit and push trades.csv to GitHub.

Usage:
    python push_trades.py
"""

import subprocess
import sys
import os
from datetime import datetime

TRADES_CSV = os.path.join(os.path.dirname(__file__), "trades.csv")


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(__file__))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        print(f"ERROR: {result.stderr.strip()}")
        sys.exit(1)


def main() -> None:
    if not os.path.exists(TRADES_CSV):
        print("trades.csv not found -- no trades recorded yet.")
        sys.exit(0)

    # Check if there's anything new to commit.
    status = subprocess.run(
        ["git", "status", "--porcelain", "trades.csv"],
        capture_output=True, text=True, cwd=os.path.dirname(__file__),
    )
    if not status.stdout.strip():
        print("trades.csv is already up to date -- nothing to push.")
        sys.exit(0)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Pushing trades.csv to GitHub ({now})...")

    run(["git", "add", "trades.csv"])
    run(["git", "commit", "-m", f"Update trades log {now}"])
    run(["git", "push"])

    print("Done. trades.csv pushed to GitHub.")


if __name__ == "__main__":
    main()
