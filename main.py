"""程序入口。

普通运行：
    .venv\\Scripts\\python.exe main.py

无窗口冒烟测试与截图：
    .venv\\Scripts\\python.exe main.py --headless --battle --demo --seconds 6 --screenshot shots/battle.png
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="造梦西游1 PyGame remake")
    p.add_argument("--headless", action="store_true", help="use SDL dummy drivers, no window")
    p.add_argument("--battle", action="store_true", help="start directly in a battle scene")
    p.add_argument("--level-id", type=str, default="1-1", help="level id, e.g. 1-1 / 1-2 / 4-1")
    p.add_argument("--demo", action="store_true", help="auto-run right/attack for screenshots")
    p.add_argument("--seconds", type=float, default=3.0, help="headless run length")
    p.add_argument("--screenshot", type=str, default=None, help="save final frame to this path")
    p.add_argument("--warp", type=float, default=None, help="debug: start player at this world x")
    p.add_argument("--level", type=int, default=None, help="debug: starting level (default 1)")
    p.add_argument("--fullscreen", action="store_true", help="start fullscreen (F11 toggles)")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--no-loading", action="store_true", help="skip the LoadingBar 15 splash")
    p.add_argument("--loading", action="store_true", help="force the LoadingBar 15 splash, even headless")
    p.add_argument("--loading-stats", action="store_true",
                   help="print actual per-bundle load seconds/bytes")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.headless:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from game.app import GameApp

    show_loading = None
    if args.no_loading:
        show_loading = False
    elif args.loading or args.loading_stats:
        show_loading = True
    app = GameApp(headless=args.headless, audio=not args.no_audio, warp=args.warp,
                  start_level=args.level, level_id=args.level_id,
                  fullscreen=args.fullscreen, show_loading=show_loading,
                  loading_stats=args.loading_stats)
    if args.headless:
        app.run_headless(args.seconds, args.screenshot, battle=args.battle, demo=args.demo)
    else:
        app.run()


if __name__ == "__main__":
    main()
