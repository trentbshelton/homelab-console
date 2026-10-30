#!/usr/bin/env python3
"""homelab-console: a number-key menu for the lab, for a small box on a CRT."""

import argparse
import curses
import os
import sys

from hlc import config, flows, ui
from hlc.backend import Backend, Demo, DryRun

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "config.toml"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true",
                      help="fake lab, nothing is contacted (uses config.example.toml if no config)")
    mode.add_argument("--dry-run", action="store_true",
                      help="real checks, but only show what reboots/power changes would run")
    ap.add_argument("--update-report", action="store_true",
                    help="print pending updates for each machine and exit (for a weekly reminder)")
    args = ap.parse_args()

    path = args.config
    if args.demo and not os.path.exists(path):
        path = os.path.join(HERE, "config.example.toml")
    try:
        cfg = config.load(path)
    except FileNotFoundError:
        sys.exit(f"No config at {path}. Copy config.example.toml to config.toml and edit it.")
    except config.ConfigError as e:
        sys.exit(f"Config problem: {e}")

    if args.demo:
        backend, name = Demo(cfg), "demo"
    elif args.dry_run:
        backend, name = DryRun(), "dry-run"
    else:
        backend, name = Backend(), "live"

    if args.update_report:
        sys.exit(update_report(cfg, backend))

    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(ui.run, cfg, backend, name)


def update_report(cfg, backend):
    """One line per machine. Exit 0 = all up to date, 1 = something waiting, 2 = a check failed."""
    worst = 0
    for step in flows.update_check_steps(cfg, backend):
        r = step.run(lambda text: None)
        print(f"{step.label.removesuffix(' updates')}: {r.detail or r.status}")
        worst = max(worst, {"FAIL": 2, "WARN": 1}.get(r.status, 0))
    return worst


if __name__ == "__main__":
    main()
