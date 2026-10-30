"""What each menu item does, as a list of steps.

Each step is a label plus a function. The UI runs them one at a time and
draws them like a server's boot screen ("Checking T320 .......... OK").
Step functions get a progress() callback for things like elapsed time.
"""

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
