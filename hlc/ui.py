"""The curses screens. Built for an 80x24 text console on a 640x480 CRT."""

import curses
import threading
import time

from . import flows
from .backend import FAIL, OK, SKIP, WARN, Result

MENU = [
    ("1", "Status details"),
    ("2", "Internet check"),
    ("3", "IP list"),
    ("4", "Restart a service"),
    ("5", "Power on a server"),
    ("6", "Reboot a server (soft)"),
    ("7", "Force power cycle (hard)"),
    ("8", "Shut down everything"),
]

DOT_COL = 50     # where the dots stop and the status word starts
SPIN = "|/-\\"


class Colors:
    def __init__(self):
        self.ok = self.warn = curses.A_NORMAL
        self.bar = curses.A_REVERSE
        self.fail = curses.A_BOLD
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_WHITE)
            self.ok = curses.color_pair(1) | curses.A_BOLD
            self.fail = curses.color_pair(2) | curses.A_BOLD
            self.warn = curses.color_pair(3) | curses.A_BOLD
            self.bar = curses.color_pair(4)
        self.dim = curses.A_DIM

    def of(self, status):
        return {OK: self.ok, FAIL: self.fail, WARN: self.warn, SKIP: self.dim}.get(status, 0)


class Summary:
    """Header info, refreshed in the background with quick checks only."""

    def __init__(self, cfg, backend):
        self.cfg, self.b = cfg, backend
        self.lock = threading.Lock()
        self.internet = None
        self.ups = None
        self.machines = {}
        self.updated = None
        self.wake = threading.Event()

    def refresh(self):
        internet = self.b.ping(self.cfg.internet_targets[0])
        ups = self.b.ups(self.cfg.ups) if self.cfg.ups else None
        machines = {m.key: self.b.ping(m.host) for m in self.cfg.machines}
        with self.lock:
            self.internet, self.ups, self.machines = internet, ups, machines
            self.updated = time.strftime("%-I:%M %p")

    def loop(self):
        while True:
            self.wake.wait(self.cfg.refresh)
            self.wake.clear()
            try:
                self.refresh()
            except Exception:
                pass


class App:
    def __init__(self, scr, cfg, backend, mode):
        self.scr, self.cfg, self.b, self.mode = scr, cfg, backend, mode
        self.c = Colors()
        self.summary = Summary(cfg, backend)
        self.last_key = time.time()
        self.blank = False

    # --- drawing helpers ---

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y >= h or x >= w - 1:
            return
        try:
            self.scr.addstr(y, x, text[: w - 1 - x], attr)
        except curses.error:
            pass

    def title_bar(self, text):
        _, w = self.scr.getmaxyx()
        tag = {"demo": "  [DEMO]", "dry-run": "  [DRY RUN]"}.get(self.mode, "")
        self.put(0, 0, f" {text}{tag}".ljust(w), self.c.bar)

    def footer(self, text):
        h, w = self.scr.getmaxyx()
        self.put(h - 1, 0, f" {text}".ljust(w), self.c.bar)

    def getkey(self, timeout_ms=500):
        """getch that keeps track of idle time and handles screen blanking."""
        self.scr.timeout(timeout_ms)
        k = self.scr.getch()
        if k == -1:
            if not self.blank and time.time() - self.last_key > self.cfg.blank_after:
                self.blank = True
                self.scr.erase()
                self.scr.refresh()
            return None
        self.last_key = time.time()
        if self.blank:
            self.blank = False      # first key only wakes the screen
            return None
        if k == curses.KEY_RESIZE:
            return None
        return k

    # --- boot-screen style step runner ---

    def run_steps(self, title, steps, intro=None):
        """Run steps one by one, drawing dots while each runs. Returns results."""
        lines = []          # (label, status, detail)
        results = []
        if intro:
            lines.append((intro, None, None))
        for step in steps:
            note = [""]
            box = {}
            t = threading.Thread(target=lambda: box.setdefault("r", _safe(step, note)), daemon=True)
            t.start()
            i = 0
            while t.is_alive():
                self.draw_steps(title, lines, (step.label, i, note[0]))
                self.scr.timeout(120)
                self.scr.getch()          # swallow keys while working
                i += 1
            r = box["r"]
            results.append(r)
            lines.append((step.label, r.status, r.detail))
            if r.status == FAIL and step.stop_on_fail:
                break
        self.draw_steps(title, lines, None)
        self.last_key = time.time()
        return results, lines

    def draw_steps(self, title, lines, running, verdict=None, foot=None):
        self.scr.erase()
        h, _ = self.scr.getmaxyx()
        self.title_bar(title)
        rows = list(lines)
        if running:
            rows.append(running)
        room = h - 4 - (2 if verdict else 0)
        rows = rows[-room:]
        y = 2
        for row in rows:
            if row is running:
                label, tick, note = row
                dots = "." * min(tick % 30 + 1, max(DOT_COL - len(label) - 3, 1))
                self.put(y, 1, f"{label} {dots}")
                spin = f" {SPIN[tick % 4]} {note}"
                self.put(y, 1 + len(label) + 1 + len(dots), spin, self.c.dim)
            else:
                label, status, detail = row
                if status is None:
                    self.put(y, 1, label)
                else:
                    self.put(y, 1, f"{label} ".ljust(DOT_COL, "."))
                    self.put(y, DOT_COL + 1, status.ljust(5), self.c.of(status))
                    if detail:
                        self.put(y, DOT_COL + 7, detail, self.c.dim)
            y += 1
        if verdict:
            status, text = verdict
            self.put(y + 1, 1, text, self.c.of(status))
        if foot:
            self.footer(foot)
        self.scr.refresh()

    def wait_any_key(self, title, lines, verdict=None):
        self.draw_steps(title, lines, None, verdict, "Press any key to go back")
        while self.getkey() is None:
            if self.blank:
                continue
        return

    # --- screens ---

    def boot(self):
        _, lines = self.run_steps(self.cfg.title, flows.boot_steps(self.cfg, self.b),
                                  intro="Starting up, checking the lab...")
        self.summary.refresh()
        threading.Thread(target=self.summary.loop, daemon=True).start()
        self.draw_steps(self.cfg.title, lines, None, foot="Press any key for the menu")
        end = time.time() + 15
        while time.time() < end and self.getkey(300) is None:
            pass

    def main_menu(self):
        while True:
            if not self.blank:
                self.draw_main()
            k = self.getkey()
            if k is None:
                continue
            ch = chr(k).lower() if 0 <= k < 256 else ""
            if ch == "1":
                self.status_details()
            elif ch == "2":
                self.internet_check()
            elif ch == "3":
                self.ip_list()
            elif ch == "4":
                self.restart_service()
            elif ch == "5":
                self.machine_action("Power on", lambda m: m.ipmi, flows.power_on_steps, 5)
            elif ch == "6":
                self.machine_action("Reboot (soft)", lambda m: m.ssh or m.truenas_key_file,
                                    flows.soft_reboot_steps, 6)
            elif ch == "7":
                self.machine_action("Force power cycle", lambda m: m.ipmi,
                                    flows.hard_cycle_steps, 7, warn=True)
            elif ch == "8":
                self.shutdown_all()
            elif ch == "r":
                self.summary.wake.set()
            elif ch == "q":
                if self.ask_pin():
                    return

    def draw_main(self):
        self.scr.erase()
        self.title_bar(self.cfg.title)
        s = self.summary
        with s.lock:
            internet, ups, machines, updated = s.internet, s.ups, dict(s.machines), s.updated
        y = 2
        x = 1
        self.put(y, x, "Internet: ")
        x += 10
        word = "..." if internet is None else ("OK" if internet.status == OK else "DOWN")
        self.put(y, x, word, self.c.of(internet.status) if internet else 0)
        x += len(word) + 4
        if ups:
            self.put(y, x, "UPS: ")
            self.put(y, x + 5, ups.detail, self.c.of(ups.status))
        if updated:
            self.put(y, 62, f"Updated {updated}", self.c.dim)

        y = 4
        for i, m in enumerate(self.cfg.machines):
            col = 1 + (i % 3) * 26
            row = y + i // 3
            r = machines.get(m.key)
            self.put(row, col, m.name[:14].ljust(15))
            word = "..." if r is None else ("UP" if r.status == OK else "DOWN")
            self.put(row, col + 15, word, self.c.of(r.status) if r else 0)
        y += (len(self.cfg.machines) + 2) // 3 + 1

        self.put(y, 1, "-" * 76, self.c.dim)
        y += 1
        half = (len(MENU) + 1) // 2
        for i, (key, label) in enumerate(MENU):
            col = 3 if i < half else 41
            self.put(y + 1 + (i % half), col, f"{key})  {label}")
        self.footer("Press a number.   R = refresh now   Q = exit")
        self.scr.refresh()

    def status_details(self):
        cfg, b = self.cfg, self.b
        steps = flows.internet_steps(cfg, b) + flows.ups_steps(cfg, b) + \
            flows.machine_steps(cfg, b) + flows.service_steps(cfg, b)
        results, lines = self.run_steps("STATUS DETAILS", steps)
        bad = sum(r.status == FAIL for r in results)
        verdict = (OK, "Everything looks good.") if not bad else \
            (FAIL, f"{bad} problem(s) found. Tell Trent which lines say FAIL.")
        self.summary.wake.set()
        self.wait_any_key("STATUS DETAILS", lines, verdict)

    def internet_check(self):
        results, lines = self.run_steps("INTERNET CHECK", flows.internet_steps(self.cfg, self.b))
        self.wait_any_key("INTERNET CHECK", lines, flows.internet_verdict(results))

    def ip_list(self):
        self.scr.erase()
        self.title_bar("IP LIST")
        self.put(2, 1, "NAME".ljust(20) + "ADDRESS".ljust(18) + "iDRAC".ljust(18) + "NOTE", curses.A_BOLD)
        y = 3
        self.put(y, 1, "Router".ljust(20) + self.cfg.router)
        for m in self.cfg.machines:
            y += 1
            idrac = m.ipmi.host if m.ipmi else "-"
            self.put(y, 1, m.name[:19].ljust(20) + m.host.ljust(18) + idrac.ljust(18) + m.note)
        self.footer("Press any key to go back")
        self.scr.refresh()
        while self.getkey() is None:
            pass

    def pick(self, title, items, warn=None):
        """Numbered list. Returns the chosen item or None."""
        if not items:
            self.message(title, WARN, "Nothing set up for this yet.")
            return None
        while True:
            if not self.blank:
                self.scr.erase()
                self.title_bar(title)
                y = 2
                if warn:
                    self.put(y, 1, warn, self.c.warn)
                    y += 2
                for i, (label, _) in enumerate(items[:9], 1):
                    self.put(y, 3, f"{i})  {label}")
                    y += 1
                self.footer("Press a number.   Any other key = go back")
                self.scr.refresh()
            k = self.getkey()
            if k is None:
                continue
            if ord("1") <= k <= ord(str(min(len(items), 9))):
                return items[k - ord("1")][1]
            return None

    def confirm(self, title, text):
        self.scr.erase()
        self.title_bar(title)
        self.put(2, 1, text, curses.A_BOLD)
        self.put(4, 1, "Press Y to do it. Any other key cancels.")
        self.scr.refresh()
        while (k := self.getkey()) is None:
            pass
        return k in (ord("y"), ord("Y"))

    def act(self, title, steps, code):
        results, lines = self.run_steps(title, steps)
        failed = [i for i, r in enumerate(results, 1) if r.status == FAIL]
        if failed:
            verdict = (FAIL, f"FAILED. Tell Trent: code {code}-{failed[0]}")
        else:
            verdict = (OK, "DONE. Tell Trent: OK")
        self.summary.wake.set()
        self.wait_any_key(title, lines, verdict)

    def machine_action(self, name, can, make_steps, code, warn=False):
        items = [(m.name, m) for m in self.cfg.machines if can(m)]
        note = "Only use this if the server is frozen. It's like pulling the plug." if warn else None
        m = self.pick(name.upper(), items, note)
        if m and self.confirm(name.upper(), f"{name}: {m.name}?"):
            self.act(f"{name.upper()}: {m.name}", make_steps(m, self.b), code)

    def restart_service(self):
        items = [(r.name, r) for r in self.cfg.restarts]
        r = self.pick("RESTART A SERVICE", items)
        if r and self.confirm("RESTART A SERVICE", f"Restart {r.name}?"):
            self.act(f"RESTART: {r.name}", flows.restart_steps(self.cfg, r, self.b), 4)

    def shutdown_all(self):
        steps = flows.shutdown_all_steps(self.cfg, self.b)
        if not steps:
            self.message("SHUT DOWN EVERYTHING", WARN, "No machines have a shutdown_order set.")
            return
        order = ", ".join(m.name for m in sorted(
            (m for m in self.cfg.machines if m.shutdown_order is not None),
            key=lambda m: m.shutdown_order))
        if not self.confirm("SHUT DOWN EVERYTHING", "Turn off every server? Order: " + order):
            return
        if not self.confirm("SHUT DOWN EVERYTHING", "Are you sure? Press Y again."):
            return
        self.act("SHUT DOWN EVERYTHING", steps, 8)

    def message(self, title, status, text):
        self.wait_any_key(title, [], (status, text))

    def ask_pin(self):
        if not self.cfg.pin:
            return True
        entered = ""
        while True:
            self.scr.erase()
            self.title_bar("EXIT")
            self.put(2, 1, "Enter PIN to exit to the shell (Esc cancels):")
            self.put(4, 3, "*" * len(entered))
            self.scr.refresh()
            k = self.getkey()
            if k is None:
                if self.blank:
                    return False
                continue
            if k == 27:
                return False
            if k in (10, 13, curses.KEY_ENTER):
                return entered == self.cfg.pin
            if k in (curses.KEY_BACKSPACE, 127, 8):
                entered = entered[:-1]
            elif 48 <= k <= 57:
                entered += chr(k)


def _safe(step, note):
    try:
        return step.run(lambda text: note.__setitem__(0, text))
    except Exception as e:
        return Result(FAIL, str(e)[:40])


def run(scr, cfg, backend, mode):
    curses.curs_set(0)
    curses.raw()            # Ctrl+C / Ctrl+Z come in as plain keys and get ignored
    scr.keypad(True)
    app = App(scr, cfg, backend, mode)
    app.boot()
    app.main_menu()
