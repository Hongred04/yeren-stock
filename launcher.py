# -*- coding: utf-8 -*-
"""ASCII-named launcher (used by start .bat).

Same behaviour as the Chinese-named entry script, but the filename is pure
ASCII so a .bat file can reference it without any encoding risk.

Usage:
    python launcher.py                 # start dashboard on port 8777
    python launcher.py --report        # console self-check report only
    python launcher.py --port 8888
    python launcher.py --no-browser
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


def banner() -> None:
    print("=" * 66)
    print("  Stock Monitor - Intelligent A-Share Trading System")
    print("  Strategy engine: 8-module decision chain")
    print("=" * 66)


def check_port(port: int) -> list[int]:
    """Return PIDs currently LISTENING on the given port."""
    pids: list[int] = []
    try:
        proc = subprocess.run(
            ["netstat", "-ano"], capture_output=True, timeout=15
        )
        out = proc.stdout.decode("gbk", errors="ignore")
        for line in out.splitlines():
            if f":{port}" not in line or "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) >= 5 and parts[-1].isdigit():
                pid = int(parts[-1])
                if pid not in pids:
                    pids.append(pid)
    except Exception as exc:  # pragma: no cover - diagnostics only
        print(f"  [warn] port check failed: {exc}")
    return pids


def kill_stale(port: int) -> bool:
    """Kill leftover instances bound to the port."""
    pids = check_port(port)
    if not pids:
        return False
    print(f"\n  Port {port} held by {len(pids)} stale process(es), cleaning...")
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
            )
            print(f"    killed PID {pid}")
        except Exception:
            pass
    time.sleep(1.5)
    return True


def diagnose(port: int) -> None:
    """Print actionable hints when the dashboard cannot be reached."""
    url = f"http://127.0.0.1:{port}/api/health"
    print(f"\n  Self-test: {url}")
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            print(f"  Server is alive: {body[:200]}")
            return
    except Exception as exc:
        print(f"  Server not reachable: {exc}")

    print("\n  Troubleshooting:")
    print("   1. Another program may occupy the port. Try:")
    print("        python launcher.py --port 8899")
    print("   2. Run the self-check report:")
    print("        python launcher.py --report")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stock Monitor launcher")
    ap.add_argument("--report", action="store_true",
                    help="run console self-check report only")
    ap.add_argument("--port", type=int, default=8777, help="dashboard port")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open the browser automatically")
    ap.add_argument("--keep-stale", action="store_true",
                    help="keep stale processes holding the port")
    ap.add_argument("--check-only", action="store_true",
                    help="start server, probe it, then exit")
    args = ap.parse_args()

    banner()

    if args.report:
        from selfcheck import main as selfcheck_main
        selfcheck_main()
        return

    if not args.keep_stale:
        kill_stale(args.port)

    import server

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  Dashboard: {url}")
    print("  Close this window to stop the service.\n")

    if not args.no_browser and not args.check_only:
        threading.Thread(
            target=lambda: (time.sleep(1.5), webbrowser.open(url)),
            daemon=True,
        ).start()

    if args.check_only:
        import threading as th

        th.Thread(target=lambda: server.main(args.port), daemon=True).start()
        time.sleep(3)
        diagnose(args.port)
        return

    try:
        server.main(args.port)
    except OSError as exc:
        print(f"\n[ERROR] failed to bind port {args.port}: {exc}")
        print("        try another port: python launcher.py --port 8899")
        diagnose(args.port)
    except KeyboardInterrupt:
        print("\n  Service stopped.")


if __name__ == "__main__":
    main()
