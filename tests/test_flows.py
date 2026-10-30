import os
import time
import unittest

from hlc import config, flows
from hlc.backend import FAIL, OK, SKIP, Demo, DryRun

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


class ConfigTests(unittest.TestCase):
    def test_restart_needs_ssh(self, tmp="/tmp/hlc-test.toml"):
        with open(tmp, "w") as f:
            f.write('[[machine]]\nkey="a"\nname="A"\nhost="1.2.3.4"\n'
                    '[[restart]]\nname="x"\nmachine="a"\ncommand="true"\n')
        with self.assertRaises(config.ConfigError):
            config.load(tmp)
        os.remove(tmp)


if __name__ == "__main__":
    unittest.main()
