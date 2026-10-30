"""Load and check config.toml."""

import tomllib
from dataclasses import dataclass, field


@dataclass
class Ipmi:
    host: str
    user: str
    password_file: str


@dataclass
class Machine:
    key: str
    name: str
    host: str
    ipmi: Ipmi | None = None
    ssh: str | None = None              # user@host, for soft reboot/shutdown
    truenas_key_file: str | None = None  # TrueNAS API key, for pool health + reboot
    truenas_cert_file: str | None = None # TrueNAS's own cert, to verify HTTPS
    shutdown_order: int | None = None   # lower shuts down first; None = skip
    boot_minutes: int = 10              # how long to wait for it to come back
    note: str = ""


@dataclass
class Service:
    name: str
    url: str


@dataclass
class Restart:
    name: str
    machine: str
    command: str
    check_url: str | None = None


@dataclass
class Config:
    title: str = "HOMELAB CONSOLE"
    pin: str = ""
    blank_after: int = 300
    refresh: int = 60
    router: str = "192.168.1.1"
    internet_targets: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    dns_name: str = "github.com"
    ups: str | None = None              # name@host[:port], read over NUT
    machines: list[Machine] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    restarts: list[Restart] = field(default_factory=list)

    def machine(self, key):
        for m in self.machines:
            if m.key == key:
                return m
        raise KeyError(key)


class ConfigError(Exception):
    pass


def load(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    con = raw.get("console", {})
    net = raw.get("internet", {})
    cfg = Config(
        title=con.get("title", Config.title),
        pin=str(con.get("pin", "")),
        blank_after=int(con.get("blank_after", 300)),
        refresh=int(con.get("refresh", 60)),
        router=net.get("router", Config.router),
        internet_targets=net.get("targets", ["1.1.1.1", "8.8.8.8"]),
        dns_name=net.get("dns_name", "github.com"),
        ups=raw.get("ups", {}).get("name"),
    )

    for m in raw.get("machine", []):
        ipmi = m.get("ipmi")
        cfg.machines.append(Machine(
            key=_need(m, "key", "machine"),
            name=_need(m, "name", "machine"),
            host=_need(m, "host", "machine"),
            ipmi=Ipmi(ipmi["host"], ipmi["user"], ipmi["password_file"]) if ipmi else None,
            ssh=m.get("ssh"),
            truenas_key_file=m.get("truenas_key_file"),
            truenas_cert_file=m.get("truenas_cert_file"),
            shutdown_order=m.get("shutdown_order"),
            boot_minutes=int(m.get("boot_minutes", 10)),
            note=m.get("note", ""),
        ))

    keys = [m.key for m in cfg.machines]
    if len(keys) != len(set(keys)):
        raise ConfigError("two machines share the same key")

    for s in raw.get("service", []):
        cfg.services.append(Service(_need(s, "name", "service"), _need(s, "url", "service")))

    for r in raw.get("restart", []):
        restart = Restart(
            name=_need(r, "name", "restart"),
            machine=_need(r, "machine", "restart"),
            command=_need(r, "command", "restart"),
            check_url=r.get("check_url"),
        )
        if restart.machine not in keys:
            raise ConfigError(f"restart '{restart.name}' points at unknown machine '{restart.machine}'")
        if not cfg.machine(restart.machine).ssh:
            raise ConfigError(f"restart '{restart.name}' needs ssh set on machine '{restart.machine}'")
        cfg.restarts.append(restart)

    return cfg


def _need(table, key, kind):
    if key not in table:
        raise ConfigError(f"a [[{kind}]] entry is missing '{key}'")
    return table[key]
