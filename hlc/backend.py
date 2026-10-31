"""Talking to the machines.

Backend does the real thing. DryRun reads for real but only prints what it
would change. Demo fakes everything, so the menu can be tried anywhere.
"""

import json
import os
import random
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

OK, FAIL, WARN, SKIP = "OK", "FAIL", "WARN", "SKIP"


@dataclass
class Result:
    status: str
    detail: str = ""

    @property
    def ok(self):
        return self.status in (OK, SKIP)


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, f"{cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    out = (p.stdout + p.stderr).strip().splitlines()
    return p.returncode == 0, out[-1] if out else ""


class Backend:
    dry_run = False

    # --- reads ---

    def ping(self, host):
        for _ in range(2):
            ok, _ = _run(["ping", "-c1", "-W2", host], timeout=5)
            if ok:
                return Result(OK)
        return Result(FAIL, "no reply")

    def tcp(self, host, port):
        try:
            socket.create_connection((host, port), 3).close()
            return Result(OK)
        except OSError as e:
            return Result(FAIL, e.strerror or "closed")

    def dns(self, name):
        try:
            socket.getaddrinfo(name, 443)
            return Result(OK)
        except OSError:
            return Result(FAIL, "lookup failed")

    def http(self, url):
        # Only checks that the app answers and sends nothing secret, so a
        # self-signed cert is fine here.
        try:
            with urllib.request.urlopen(url, timeout=5, context=_unverified()) as r:
                return Result(OK, str(r.status))
        except urllib.error.HTTPError as e:
            # A login page or redirect still means the app is up.
            return Result(OK if e.code < 500 else FAIL, str(e.code))
        except Exception as e:
            return Result(FAIL, _short(e))

    def ipmi_status(self, ipmi):
        ok, out = _run(self._ipmi(ipmi, "status"))
        if not ok:
            return Result(FAIL, out or "iDRAC not answering")
        return Result(OK, "on" if out.endswith("on") else "off")

    def ups(self, spec):
        name, _, host = spec.partition("@")
        host, _, port = host.partition(":")
        want = ["ups.status", "battery.charge", "battery.runtime"]
        try:
            with socket.create_connection((host, int(port or 3493)), 3) as s:
                f = s.makefile("rw")
                vals = {}
                for var in want:
                    f.write(f"GET VAR {name} {var}\n")
                    f.flush()
                    line = f.readline().strip()
                    if line.startswith("VAR"):
                        vals[var] = line.split('"')[1]
        except OSError as e:
            return Result(FAIL, e.strerror or "no answer")
        status = vals.get("ups.status", "?")
        charge = vals.get("battery.charge", "?")
        mins = int(vals.get("battery.runtime", "0")) // 60
        text = f"{charge}% {'on power' if 'OL' in status else 'ON BATTERY'} ~{mins} min"
        return Result(OK if "OL" in status else WARN, text)

    def ssh_read(self, target, command, timeout=120):
        """Runs even in dry run, so only use it for commands that change nothing."""
        ok, out = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                        target, command], timeout)
        return Result(OK if ok else FAIL, out)

    def truenas_get(self, host, key_file, cert_file, path):
        return self._truenas(host, key_file, cert_file, "GET", path)

    def truenas_pools(self, host, key_file, cert_file=None):
        try:
            pools = self._truenas(host, key_file, cert_file, "GET", "/pool")
        except Exception as e:
            return Result(FAIL, _short(e))
        bad = [p["name"] for p in pools if not p.get("healthy")]
        names = ", ".join(f"{p['name']} {p['status'].lower()}" for p in pools)
        if bad:
            return Result(FAIL, f"unhealthy: {', '.join(bad)}")
        return Result(OK, names or "no pools")

    # --- changes ---

    def ipmi_power(self, ipmi, action):
        """action: on, cycle, soft (ACPI shutdown, like a short press)."""
        return self._change(self._ipmi(ipmi, action))

    def ssh(self, target, command, timeout=60):
        return self._change(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                             target, command], timeout=timeout)

    def truenas_call(self, host, key_file, cert_file, path, body):
        if self.dry_run:
            return Result(OK, f"dry run: POST {path}")
        try:
            self._truenas(host, key_file, cert_file, "POST", path, body)
            return Result(OK)
        except Exception as e:
            return Result(FAIL, _short(e))

    def sleep(self, seconds):
        time.sleep(seconds)

    # --- helpers ---

    def _change(self, cmd, timeout=30):
        if self.dry_run:
            return Result(OK, "dry run: " + " ".join(cmd))
        ok, out = _run(cmd, timeout)
        return Result(OK if ok else FAIL, out)

    def _ipmi(self, ipmi, action):
        # Ask for OPERATOR, not ipmitool's default ADMINISTRATOR: it's all that
        # power control needs, and an operator-only user is refused otherwise.
        return ["ipmitool", "-I", "lanplus", "-L", "OPERATOR", "-H", ipmi.host, "-U", ipmi.user,
                "-f", ipmi.password_file, "chassis", "power", action]

    def _truenas(self, host, key_file, cert_file, method, path, body=None):
        with open(key_file) as f:
            key = f.read().strip()
        if cert_file:
            # Trust exactly TrueNAS's own self-signed cert. It's issued to a
            # name, not the IP, so only the hostname check is skipped.
            ctx = ssl.create_default_context(cafile=cert_file)
            ctx.check_hostname = False
        else:
            ctx = _unverified()
        # HTTPS only: TrueNAS revokes an API key that is ever sent over plain HTTP.
        req = urllib.request.Request(
            f"https://{host}/api/v2.0{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return json.load(r)


class DryRun(Backend):
    dry_run = True


class Demo(Backend):
    """Fake lab: slow-ish answers, machines really go down and come back."""

    def __init__(self, cfg):
        self.ipmi_hosts = {m.ipmi.host: m.host for m in cfg.machines if m.ipmi}
        self.down_until = {}   # host -> time it answers again
        # Hosts that start out down, e.g. HLC_DEMO_OFF="1.1.1.1 8.8.8.8"
        self.off = set(os.environ.get("HLC_DEMO_OFF", "").split())
        self.updated = set()   # hosts that ran updates
        self.kernel = set()    # hosts whose updates include a new kernel

    def _wait(self):
        time.sleep(random.uniform(0.3, 1.2))

    def _up(self, host):
        return host not in self.off and time.time() >= self.down_until.get(host, 0)

    def ping(self, host):
        self._wait()
        return Result(OK) if self._up(host) else Result(FAIL, "no reply")

    def tcp(self, host, port):
        return self.ping(host)

    def dns(self, name):
        self._wait()
        return Result(OK)

    def http(self, url):
        self._wait()
        return Result(OK, "200")

    def ipmi_status(self, ipmi):
        self._wait()
        return Result(OK, "off" if self._by_ipmi(ipmi) in self.off else "on")

    def ups(self, spec):
        self._wait()
        return Result(OK, "100% on power ~57 min")

    def truenas_pools(self, host, key_file, cert_file=None):
        self._wait()
        return Result(OK, "backup online")

    def ipmi_power(self, ipmi, action):
        self._wait()
        host = self._by_ipmi(ipmi)
        if action == "soft":
            self.off.add(host)
        else:
            self.off.discard(host)
            self.down_until[host] = time.time() + 6
        return Result(OK)

    def ssh_read(self, target, command, timeout=120):
        self._wait()
        if "PENDING=" not in command:
            return Result(OK)
        host = target.split("@")[-1]
        if host in self.updated:
            return Result(OK, f"PENDING=0 REBOOT={int(host in self.kernel)} APTFAIL=0 LAST=today")
        n = sum(map(ord, host)) % 40 + 1    # same answer every time, for screenshots
        if n > 10:
            self.kernel.add(host)
        return Result(OK, f"PENDING={n} REBOOT=0 APTFAIL=0 LAST=2026-10-12")

    def truenas_get(self, host, key_file, cert_file, path):
        self._wait()
        return {"code": "NORMAL", "status": {"new_version": None}}

    def ssh(self, target, command, timeout=60):
        self._wait()
        host = target.split("@")[-1]
        if "dist-upgrade" in command or "compose" in command:
            self.sleep(3)
            self.updated.add(host)
            return Result(OK, "12 upgraded, 1 newly installed, 0 to remove"
                          if "dist-upgrade" in command else "8 projects updated")
        if "poweroff" in command:
            self.off.add(host)
        elif "reboot" in command:
            self.down_until[host] = time.time() + 8
            self.kernel.discard(host)
        return Result(OK)

    def truenas_call(self, host, key_file, cert_file, path, body):
        self._wait()
        if path.endswith("shutdown"):
            self.off.add(host)
        else:
            self.down_until[host] = time.time() + 8
        return Result(OK)

    def _by_ipmi(self, ipmi):
        return self.ipmi_hosts.get(ipmi.host, ipmi.host)

    def sleep(self, seconds):
        time.sleep(min(seconds, 1))


def _unverified():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _short(e):
    reason = getattr(e, "reason", None)
    text = str(reason if reason is not None else e)
    return text[:40]
