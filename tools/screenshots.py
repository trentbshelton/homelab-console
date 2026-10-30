#!/usr/bin/env python3
"""Make the README screenshots from the demo.

Runs `console.py --demo` inside an 80x24 tmux window, presses keys, and
renders what's on screen to PNG/GIF. Needs tmux, Pillow and pyte
(pip install pillow pyte). Only ever uses demo mode and the example config.

    python3 tools/screenshots.py
"""

import os
import subprocess
import time

import pyte
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "screenshots")
SESSION = "hlc-shots"
COLS, ROWS = 80, 24
FONT = "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"
SIZE = 16
PAD = 16

# Standard VGA text-mode colors.
NORMAL = {"black": (0, 0, 0), "red": (170, 0, 0), "green": (0, 170, 0),
          "brown": (170, 85, 0), "yellow": (170, 85, 0), "blue": (0, 0, 170),
          "magenta": (170, 0, 170), "cyan": (0, 170, 170), "white": (170, 170, 170)}
BRIGHT = {"black": (85, 85, 85), "red": (255, 85, 85), "green": (85, 255, 85),
          "brown": (255, 255, 85), "yellow": (255, 255, 85), "blue": (85, 85, 255),
          "magenta": (255, 85, 255), "cyan": (85, 255, 255), "white": (255, 255, 255)}
FG, BG = (200, 200, 200), (0, 0, 0)

font = ImageFont.truetype(FONT, SIZE)
bold = ImageFont.truetype(FONT_BOLD, SIZE)
CW = round(font.getlength("M"))
CH = SIZE + 4


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout


def grab():
    """Current tmux screen as a pyte screen."""
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    text = tmux("capture-pane", "-e", "-p", "-t", SESSION)
    stream.feed(text.rstrip("\n").replace("\n", "\r\n"))
    return screen


def color(name, bright, default):
    if name == "default":
        return default
    table = BRIGHT if bright else NORMAL
    return table.get(name, default)


def render(screen):
    img = Image.new("RGB", (COLS * CW + PAD * 2, ROWS * CH + PAD * 2), BG)
    d = ImageDraw.Draw(img)
    for y in range(ROWS):
        row = screen.buffer[y]
        for x in range(COLS):
            ch = row[x]
            fg = color(ch.fg, ch.bold, FG)
            bg = color(ch.bg, False, BG)
            if ch.reverse:
                fg, bg = (bg if ch.bg != "default" else BG), (fg if ch.fg != "default" else FG)
            px, py = PAD + x * CW, PAD + y * CH
            if bg != BG:
                d.rectangle([px, py, px + CW - 1, py + CH - 1], fill=bg)
            if ch.data.strip():
                d.text((px, py + 2), ch.data, font=bold if ch.bold else font, fill=fg)
    return img


def save_png(name):
    render(grab()).save(os.path.join(OUT, name))
    print("wrote", name)


def record_gif(name, seconds, until=None, hold=2.5):
    """Grab frames every 0.15s. Stops early once `until` text shows up."""
    frames, last = [], None
    end = time.time() + seconds
    while time.time() < end:
        screen = grab()
        text = "\n".join(screen.display)
        if text != last:
            frames.append(render(screen).quantize(colors=32))
            last = text
        if until and until in text:
            break
        time.sleep(0.15)
    durations = [150] * (len(frames) - 1) + [int(hold * 1000)]
    frames[0].save(os.path.join(OUT, name), save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print("wrote", name, f"({len(frames)} frames)")


def keys(*k, wait=0.6):
    for key in k:
        tmux("send-keys", "-t", SESSION, key)
        time.sleep(wait)


def start(off=""):
    tmux("kill-session", "-t", SESSION)
    cmd = (f"cd {ROOT} && HLC_DEMO_OFF='{off}' python3 console.py --demo "
           f"--config {os.path.join(ROOT, 'config.example.toml')}; sleep 600")
    tmux("new-session", "-d", "-s", SESSION, "-x", str(COLS), "-y", str(ROWS),
         "env", "TERM=xterm-256color", "bash", "-c", cmd)


def main():
    os.makedirs(OUT, exist_ok=True)
    start()
    try:
        time.sleep(0.3)
        record_gif("boot.gif", 30, until="Press any key for the menu", hold=4)
        keys("x", wait=1.5)
        save_png("menu.png")

        keys("1")
        record_gif("status.gif", 30, until="Press any key to go back", hold=4)
        save_png("status.png")
        keys("x")

        keys("2", wait=8)
        save_png("internet.png")
        keys("x")

        keys("3")
        save_png("ip-list.png")
        keys("x")

        keys("7")
        save_png("power-cycle-pick.png")
        keys("2")
        save_png("power-cycle-confirm.png")
        keys("y", wait=0.1)
        record_gif("power-cycle.gif", 40, until="Tell Trent", hold=4)
        keys("x")

        # Something broken: the Docker VM is down, and later the internet.
        start(off="192.168.1.30")
        time.sleep(25)
        keys("x", wait=1.5)
        save_png("menu-problem.png")
        keys("1", wait=14)
        save_png("status-problem.png")

        start(off="1.1.1.1 8.8.8.8")
        time.sleep(25)
        keys("x", "2", wait=1.5)
        time.sleep(6)
        save_png("internet-down.png")
    finally:
        tmux("kill-session", "-t", SESSION)


if __name__ == "__main__":
    main()
