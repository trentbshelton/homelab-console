"""What each menu item does, as a list of steps.

Each step is a label plus a function. The UI runs them one at a time and
draws them like a server's boot screen ("Checking T320 .......... OK").
Step functions get a progress() callback for things like elapsed time.
"""

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .backend import FAIL, OK, SKIP, WARN, Result


@dataclass
class Step:
    label: str
    run: Callable[[Callable[[str], None]], Result]
    stop_on_fail: bool = True


def wait_for(backend, host, want_up, minutes, progress):
    """Ping until the host is up (or down). Returns a Result."""
    if backend.dry_run:
        return Result(SKIP, "dry run")
    start = time.time()
    while time.time() - start < minutes * 60:
        up = backend.ping(host).status == OK
        if up == want_up:
            return Result(OK, f"{int(time.time() - start)}s")
        secs = int(time.time() - start)
        progress(f"{secs // 60}:{secs % 60:02d}")
        backend.sleep(5)
    return Result(FAIL, f"not {'up' if want_up else 'down'} after {minutes} min")


# --- checks ---

def internet_steps(cfg, b):
    steps = [Step(f"Router ({cfg.router})", lambda p: b.ping(cfg.router), False)]
    for t in cfg.internet_targets:
        steps.append(Step(f"Internet ({t})", lambda p, t=t: b.ping(t), False))
    steps.append(Step(f"DNS ({cfg.dns_name})", lambda p: b.dns(cfg.dns_name), False))
    return steps


def internet_verdict(results):
    router, *rest = results
    dns = rest[-1]
    pings = rest[:-1]
    if router.status != OK:
        return FAIL, "The router is not answering. Check the router and its cables."
    if not any(r.status == OK for r in pings):
        return FAIL, "The router is fine but the internet is down. It's AT&T, not the servers."
    if dns.status != OK:
        return WARN, "Internet works but name lookups fail (DNS problem)."
    return OK, "The internet is working."


def machine_steps(cfg, b):
    steps = []
    for m in cfg.machines:
        steps.append(Step(f"{m.name} ({m.host})", lambda p, m=m: b.ping(m.host), False))
        if m.ipmi:
            steps.append(Step(f"{m.name} iDRAC power", lambda p, m=m: _power(b, m), False))
        if m.truenas_key_file:
            steps.append(Step(f"{m.name} storage pools",
                              lambda p, m=m: b.truenas_pools(m.host, m.truenas_key_file,
                                                             m.truenas_cert_file), False))
    return steps


def _power(b, m):
    r = b.ipmi_status(m.ipmi)
    if r.status == OK and r.detail == "off":
        return Result(WARN, "powered OFF")
    return r


def service_steps(cfg, b):
    return [Step(s.name, lambda p, s=s: b.http(s.url), False) for s in cfg.services]


def ups_steps(cfg, b):
    if not cfg.ups:
        return []
    return [Step("UPS battery", lambda p: b.ups(cfg.ups), False)]


def boot_steps(cfg, b):
    return internet_steps(cfg, b) + ups_steps(cfg, b) + machine_steps(cfg, b) + service_steps(cfg, b)


# --- actions ---

def power_on_steps(m, b):
    def check(p):
        r = b.ipmi_status(m.ipmi)
        if r.status == OK and r.detail == "on":
            return Result(FAIL, "it's already on")
        return r
    return [
        Step("Asking iDRAC for power state", check),
        Step("Sending power on", lambda p: b.ipmi_power(m.ipmi, "on")),
        Step(f"Waiting for {m.name} to boot", lambda p: wait_for(b, m.host, True, m.boot_minutes, p)),
    ]


def soft_reboot_steps(m, b):
    if m.truenas_key_file:
        send = Step("Sending reboot (TrueNAS)", lambda p: b.truenas_call(
            m.host, m.truenas_key_file, m.truenas_cert_file, "/system/reboot",
            {"reason": "homelab-console", "options": {}}))
    else:
        send = Step("Sending reboot command", lambda p: b.ssh(m.ssh, "systemctl reboot"))
    return [
        Step(f"Checking {m.name} answers", lambda p: b.ping(m.host)),
        send,
        Step(f"Waiting for {m.name} to go down", lambda p: wait_for(b, m.host, False, 3, p)),
        Step(f"Waiting for {m.name} to come back", lambda p: wait_for(b, m.host, True, m.boot_minutes, p)),
    ]


def hard_cycle_steps(m, b):
    return [
        Step("Checking iDRAC answers", lambda p: b.ipmi_status(m.ipmi)),
        Step("Sending power cycle", lambda p: b.ipmi_power(m.ipmi, "cycle")),
        Step(f"Waiting for {m.name} to come back", lambda p: wait_for(b, m.host, True, m.boot_minutes, p)),
    ]


def restart_steps(cfg, r, b):
    m = cfg.machine(r.machine)
    steps = [
        Step(f"Connecting to {m.name}", lambda p: b.tcp(m.host, 22)),
        Step(f"Restarting {r.name}", lambda p: b.ssh(m.ssh, r.command)),
    ]
    if r.check_url:
        def check(p):
            res = b.http(r.check_url)
            for _ in range(5):
                if res.status == OK:
                    break
                b.sleep(5)
                res = b.http(r.check_url)
            return res
        steps.append(Step(f"Checking {r.name} answers", check))
    return steps


def shutdown_all_steps(cfg, b):
    steps = []
    order = sorted((m for m in cfg.machines if m.shutdown_order is not None),
                   key=lambda m: m.shutdown_order)
    for m in order:
        steps.append(Step(f"Shutting down {m.name}", lambda p, m=m: _shutdown(b, m), False))
        steps.append(Step(f"Waiting for {m.name} to turn off",
                          lambda p, m=m: wait_for(b, m.host, False, 5, p), False))
    return steps


def _shutdown(b, m):
    if b.ping(m.host).status != OK:
        return Result(SKIP, "already off")
    if m.truenas_key_file:
        return b.truenas_call(m.host, m.truenas_key_file, m.truenas_cert_file,
                              "/system/shutdown", {"reason": "homelab-console", "options": {}})
    if m.ssh:
        return b.ssh(m.ssh, "systemctl poweroff")
    if m.ipmi:
        return b.ipmi_power(m.ipmi, "soft")
    return Result(FAIL, "no way to shut it down")


# --- updates ---

APT = "apt-get -o Acquire::ForceIPv4=true"

# One line back: PENDING=12 REBOOT=1 APTFAIL=0 LAST=2026-10-26
# REBOOT is set by Debian/Ubuntu's flag file, or when a newer kernel is
# installed than the one running (Proxmox doesn't write the flag file).
CHECK = (
    f"f=0; {APT} -qq update >/dev/null 2>&1 || f=1; "
    "n=$(apt-get -s dist-upgrade 2>/dev/null | grep -c '^Inst'); "
    "r=0; [ -f /var/run/reboot-required ] && r=1; "
    "k=$(ls -1 /boot/vmlinuz-* 2>/dev/null | sort -V | tail -n1); "
    '[ -n "$k" ] && [ "$k" != "/boot/vmlinuz-$(uname -r)" ] && r=1; '
    "l=$(awk '/^Start-Date/{d=$2} /^Upgrade:/{u=d} END{print u}' /var/log/apt/history.log 2>/dev/null); "
    'echo "PENDING=$n REBOOT=$r APTFAIL=$f LAST=${l:-unknown}"'
)

# Keeps existing config files when a package ships a new one. Last line
# back is apt's "N upgraded, N newly installed..." summary.
INSTALL = (
    "export DEBIAN_FRONTEND=noninteractive; L=/var/log/homelab-console-update.log; "
    f"{{ date; {APT} -q update && {APT} -y -o Dpkg::Options::=--force-confdef "
    "-o Dpkg::Options::=--force-confold dist-upgrade && apt-get -y autoremove; } >$L 2>&1; "
    "rc=$?; grep 'upgraded,' $L | head -n1; exit $rc"
)


def containers_command(u):
    """Pull and restart every compose project in u.containers that is running.

    Projects that aren't running (on-demand tools) are left alone, and so
    is anything in u.skip. Images built locally get rebuilt on a fresh base.
    """
    skip = " ".join(u.skip)
    return (
        f"cd {u.containers} || exit 1; n=0; bad=''; "
        "for d in */; do p=${d%/}; "
        f"case \" {skip} \" in *\" $p \"*) continue;; esac; "
        "[ -n \"$(cd $p && docker compose ps -q 2>/dev/null)\" ] || continue; "
        "if (cd $p && docker compose build --pull -q >/dev/null 2>&1; "
        "docker compose pull -q --ignore-buildable && docker compose up -d --quiet-pull >/dev/null 2>&1); "
        "then n=$((n+1)); else bad=\"$bad $p\"; fi; done; "
        "docker image prune -f >/dev/null 2>&1; "
        "if [ -n \"$bad\" ]; then echo \"failed:$bad\"; exit 1; fi; "
        "echo \"$n projects updated\""
    )


def parse_check(text):
    """'PENDING=3 REBOOT=0 ...' -> {'PENDING': '3', ...}. Missing keys are absent."""
    out = {}
    for part in text.split():
        k, _, v = part.partition("=")
        if v:
            out[k] = v
    return out


def updatable(cfg):
    return sorted((m for m in cfg.machines if m.update), key=lambda m: m.update.order)


def installable(cfg):
    """TrueNAS updates go through its own web UI, so only apt machines here."""
    return [m for m in updatable(cfg) if m.ssh and not m.truenas_key_file]


def check_updates(b, m):
    if m.truenas_key_file:
        try:
            st = b.truenas_get(m.host, m.truenas_key_file, m.truenas_cert_file, "/update/status")
        except Exception as e:
            return Result(FAIL, str(e)[:40])
        new = (st.get("status") or {}).get("new_version")
        if new:
            return Result(WARN, f"{new.get('version', 'new version')} (web UI)")
        return Result(OK, "up to date")
    r = b.ssh_read(m.ssh, CHECK)
    if r.status != OK:
        return Result(FAIL, r.detail[:40] or "can't log in")
    v = parse_check(r.detail)
    if "PENDING" not in v:
        return Result(FAIL, "odd answer")
    if v.get("APTFAIL") == "1":
        return Result(FAIL, "can't reach update servers")
    n = int(v["PENDING"])
    bits = [f"{n} update{'s' if n != 1 else ''}" if n else "up to date"]
    if v.get("REBOOT") == "1":
        bits.append("reboot needed")
    bits.append(f"last {_short_date(v.get('LAST', ''))}")
    return Result(WARN if n or v.get("REBOOT") == "1" else OK, ", ".join(bits))


def _short_date(d):
    # 2026-10-26 -> 10-26
    return d[5:] if len(d) == 10 and d[4] == "-" else d


def update_check_steps(cfg, b):
    return [Step(f"{m.name} updates", lambda p, m=m: check_updates(b, m), False)
            for m in updatable(cfg)]


def update_install_steps(cfg, b, needs_reboot):
    """Install everything, in update order. Machines that need a reboot
    afterwards are appended to needs_reboot (the list is filled in as the
    steps run)."""
    steps = []
    for m in installable(cfg):
        steps.append(Step(f"{m.name}: system packages",
                          lambda p, m=m: _apt_summary(_clock(p, b, lambda: b.ssh(m.ssh, INSTALL, 1800))),
                          False))
        if m.update.containers:
            steps.append(Step(f"{m.name}: containers",
                              lambda p, m=m: _clock(p, b, lambda: b.ssh(
                                  m.ssh, containers_command(m.update), 1800)), False))
    for m in installable(cfg):
        steps.append(Step(f"{m.name}: reboot needed?",
                          lambda p, m=m: _reboot_check(b, m, needs_reboot), False))
    return steps


def _apt_summary(r):
    """'12 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.' -> '12 upgraded, 1 new'"""
    m = re.search(r"(\d+) upgraded, (\d+) newly installed", r.detail)
    if r.status != OK or not m:
        return r
    up, new = m.groups()
    return Result(r.status, f"{up} upgraded" + (f", {new} new" if new != "0" else ""))


def _reboot_check(b, m, needs_reboot):
    if b.dry_run:
        return Result(SKIP, "dry run")
    r = b.ssh_read(m.ssh, CHECK)
    if r.status != OK:
        return Result(FAIL, r.detail[:40] or "can't log in")
    if parse_check(r.detail).get("REBOOT") == "1":
        needs_reboot.append(m)
        return Result(WARN, "yes")
    return Result(OK, "no")


def update_reboot_steps(machines, b):
    steps = []
    for m in sorted(machines, key=lambda m: m.update.order):
        steps += soft_reboot_steps(m, b)
    return steps


def _clock(progress, b, fn):
    """Run fn in the background, showing elapsed time, since apt can take a while."""
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", fn()), daemon=True)
    start = time.time()
    t.start()
    while t.is_alive():
        secs = int(time.time() - start)
        progress(f"{secs // 60}:{secs % 60:02d}")
        t.join(1)
    return box.get("r", Result(FAIL, "crashed"))
