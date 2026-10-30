import os
import time
import unittest

from hlc import config, flows
from hlc.backend import FAIL, OK, SKIP, WARN, Demo, DryRun

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "config.example.toml")


def run(steps):
    results = []
    for s in steps:
        r = s.run(lambda text: None)
        results.append(r)
        if r.status == FAIL and s.stop_on_fail:
            break
    return results


class Fast(Demo):
    def _wait(self):
        pass


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(EXAMPLE)
        self.b = Fast(self.cfg)

    def test_example_config_loads(self):
        self.assertEqual([m.key for m in self.cfg.machines], ["pve", "nas", "docker"])
        self.assertEqual(self.cfg.restarts[0].machine, "docker")

    def test_boot_checks_all_pass(self):
        self.assertTrue(all(r.status == OK for r in run(flows.boot_steps(self.cfg, self.b))))

    def test_hard_cycle_waits_for_machine(self):
        m = self.cfg.machine("nas")
        start = time.time()
        results = run(flows.hard_cycle_steps(m, self.b))
        self.assertEqual([r.status for r in results], [OK, OK, OK])
        self.assertGreater(time.time() - start, 4)   # demo machine is down ~6s

    def test_power_on_refuses_when_already_on(self):
        results = run(flows.power_on_steps(self.cfg.machine("pve"), self.b))
        self.assertEqual(results[0].status, FAIL)
        self.assertEqual(len(results), 1)

    def test_shutdown_all_in_order(self):
        steps = flows.shutdown_all_steps(self.cfg, self.b)
        labels = [s.label for s in steps if s.label.startswith("Shutting")]
        self.assertEqual(labels, ["Shutting down Docker VM", "Shutting down Proxmox",
                                  "Shutting down NAS"])
        self.assertTrue(all(r.ok for r in run(steps)))

    def test_internet_verdict(self):
        ok, fail = flows.Result(OK), flows.Result(FAIL)
        self.assertEqual(flows.internet_verdict([ok, ok, ok, ok])[0], OK)
        self.assertIn("AT&T", flows.internet_verdict([ok, fail, fail, fail])[1])
        self.assertIn("router", flows.internet_verdict([fail, fail, fail, fail])[1])

    def test_dry_run_changes_nothing(self):
        b = DryRun()
        m = self.cfg.machine("nas")
        r = b.ipmi_power(m.ipmi, "cycle")
        self.assertIn("dry run", r.detail)
        self.assertEqual(flows.wait_for(b, m.host, True, 1, lambda t: None).status, SKIP)


class Patchy(Fast):
    """Demo lab where every apt machine has 5 updates and one includes a kernel."""

    def ssh_read(self, target, command, timeout=120):
        host = target.split("@")[-1]
        if host in self.updated:
            return flows.Result(OK, f"PENDING=0 REBOOT={int(host == '192.168.1.10')} "
                                    "APTFAIL=0 LAST=2026-10-30")
        return flows.Result(OK, "PENDING=5 REBOOT=0 APTFAIL=0 LAST=2026-10-12")

    def sleep(self, seconds):
        pass


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(EXAMPLE)
        self.b = Patchy(self.cfg)

    def test_parse_check(self):
        v = flows.parse_check("PENDING=12 REBOOT=1 APTFAIL=0 LAST=2026-10-26")
        self.assertEqual(v, {"PENDING": "12", "REBOOT": "1", "APTFAIL": "0", "LAST": "2026-10-26"})

    def test_check_shows_pending(self):
        results = run(flows.update_check_steps(self.cfg, self.b))
        self.assertEqual([r.status for r in results], [WARN, OK, WARN])   # docker, nas, pve
        self.assertEqual(results[0].detail, "5 updates, last 10-12")
        self.assertEqual(results[1].detail, "up to date")

    def test_check_flags_unreachable_mirrors(self):
        self.b.ssh_read = lambda *a, **k: flows.Result(OK, "PENDING=0 REBOOT=0 APTFAIL=1 LAST=x")
        r = flows.check_updates(self.b, self.cfg.machine("pve"))
        self.assertEqual(r.status, FAIL)

    def test_install_order_skips_truenas(self):
        needs = []
        steps = flows.update_install_steps(self.cfg, self.b, needs)
        self.assertEqual([s.label for s in steps], [
            "Docker VM: system packages", "Docker VM: containers",
            "Proxmox: system packages",
            "Docker VM: reboot needed?", "Proxmox: reboot needed?"])

    def test_install_reports_what_needs_reboot(self):
        needs = []
        results = run(flows.update_install_steps(self.cfg, self.b, needs))
        self.assertTrue(all(r.ok or r.status == WARN for r in results))
        self.assertEqual([m.key for m in needs], ["pve"])
        self.assertEqual(results[0].detail, "12 upgraded, 1 new")
        reboot = [s.label for s in flows.update_reboot_steps(needs, self.b)]
        self.assertIn("Waiting for Proxmox to come back", reboot)

    def test_dry_run_installs_nothing(self):
        needs = []
        b = DryRun()
        steps = flows.update_install_steps(self.cfg, b, needs)
        r = steps[0].run(lambda t: None)
        self.assertIn("dry run", r.detail)
        self.assertEqual(steps[-1].run(lambda t: None).status, SKIP)
        self.assertEqual(needs, [])

    def test_containers_command_skips(self):
        cmd = flows.containers_command(self.cfg.machine("docker").update)
        self.assertIn("cd /opt/services", cmd)
        self.assertIn('case " vpn " in', cmd)


class ConfigTests(unittest.TestCase):
    def test_restart_needs_ssh(self, tmp="/tmp/hlc-test.toml"):
        with open(tmp, "w") as f:
            f.write('[[machine]]\nkey="a"\nname="A"\nhost="1.2.3.4"\n'
                    '[[restart]]\nname="x"\nmachine="a"\ncommand="true"\n')
        with self.assertRaises(config.ConfigError):
            config.load(tmp)
        os.remove(tmp)

    def test_update_needs_a_way_in(self, tmp="/tmp/hlc-test2.toml"):
        with open(tmp, "w") as f:
            f.write('[[machine]]\nkey="a"\nname="A"\nhost="1.2.3.4"\nupdate={order=1}\n')
        with self.assertRaises(config.ConfigError):
            config.load(tmp)
        os.remove(tmp)


if __name__ == "__main__":
    unittest.main()
