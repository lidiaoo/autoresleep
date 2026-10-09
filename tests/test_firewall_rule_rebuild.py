"""Firewall rules must be replaced on every install, and fully cleared on uninstall.

Both halves of this were measured broken on a real machine (S7.6):
  * `firewall_apply` ran `netsh add rule` unconditionally. netsh does not object to an existing
    rule with the same name - it adds a SECOND one - so after a few installs every rule name had
    four copies (block count per name, measured: 4, and the number grew with each install);
  * its success test was `if _run(["netsh", ..., "show", "rule", "name=" + name]).strip()`, which is
    ALWAYS true: when nothing matches, netsh still prints a non-empty "no rules match" line. So a
    failed add was reported as an applied rule.
  * `firewall_remove` deleted one copy and then tested whether the NAME was still present; with
    duplicates it always was, so uninstall reported nothing removed and left the WoL ports open.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.system import windows as win  # noqa: E402

NO_MATCH = "\r\n没有与指定的标准匹配的规则。\r\n"
BLOCK = "\r\n规则名:  %s\r\n------------------------------------------------------\r\n启用:  是\r\n"


class FakeNetsh:
    """A netsh that behaves like the real one where it matters (no shell, name= parsing)."""

    def __init__(self, copies=None, add_works=True):
        self.copies = dict(copies or {})
        self.add_works = add_works

    def _name(self, argv):
        for a in argv:
            if a.startswith("name="):
                return a.split("=", 1)[1]
        return ""

    def __call__(self, cmd, timeout=None):
        argv = [str(c) for c in cmd]
        name = self._name(argv)
        if "show" in argv and "rule" in argv:
            n = self.copies.get(name, 0)
            return NO_MATCH if not n else BLOCK % name * n
        if "delete" in argv:
            self.copies[name] = max(0, self.copies.get(name, 0) - 1)
            return "\r\n确定。\r\n"
        if "add" in argv:
            if self.add_works:
                self.copies[name] = self.copies.get(name, 0) + 1
            return "\r\n确定。\r\n"
        return ""

    def total(self):
        return sum(self.copies.values())


def apply(netsh, ports=(9, 7), marker=45678, program="C:/pw.exe"):
    with mock.patch.object(win, "_run", side_effect=netsh):
        sys_obj = object.__new__(win.WindowsSystem)   # these two methods touch no instance state
        return sys_obj.firewall_apply(list(ports), marker, program)


def remove(netsh):
    with mock.patch.object(win, "_run", side_effect=netsh):
        return object.__new__(win.WindowsSystem).firewall_remove()


class TestApply(unittest.TestCase):
    def test_a_clean_slate_gets_exactly_one_rule_per_port(self):
        netsh = FakeNetsh()
        done = apply(netsh)
        self.assertEqual(len(done), 3, done)
        self.assertEqual(netsh.total(), 3)
        self.assertTrue(all(n == 1 for n in netsh.copies.values()), netsh.copies)

    def test_an_existing_rule_is_replaced_not_stacked(self):
        """The measured bug: four copies per name, growing with every install."""
        netsh = FakeNetsh({win.RULE_PREFIX + str(p): 4 for p in (9, 7, 67, 68, 45678)})
        apply(netsh, ports=(9, 7, 67, 68))
        self.assertEqual(netsh.total(), 5, "duplicates were not swept: %s" % netsh.copies)
        for port in (9, 7, 67, 68, 45678):
            self.assertEqual(netsh.copies[win.RULE_PREFIX + str(port)], 1, netsh.copies)

    def test_ports_that_are_not_configured_are_left_alone(self):
        netsh = FakeNetsh({win.RULE_PREFIX + str(p): 4 for p in (67, 68)})
        apply(netsh, ports=(9, 7))
        self.assertEqual(netsh.copies[win.RULE_PREFIX + "67"], 4)
        self.assertEqual(netsh.copies[win.RULE_PREFIX + "68"], 4)

    def test_a_failed_add_is_not_reported_as_an_applied_rule(self):
        """The old truthiness test called this success, because the 'no match' line is not empty."""
        netsh = FakeNetsh(add_works=False)
        self.assertEqual(apply(netsh), [])

    def test_the_no_match_message_is_not_mistaken_for_a_rule(self):
        netsh = FakeNetsh()
        with mock.patch.object(win, "_run", side_effect=netsh):
            self.assertEqual(win._firewall_blocks("AutoReSleep-WoL-UDP-9"), 0)
            netsh.copies["AutoReSleep-WoL-UDP-9"] = 2
            self.assertEqual(win._firewall_blocks("AutoReSleep-WoL-UDP-9"), 2)


class TestRemove(unittest.TestCase):
    def test_uninstall_clears_every_copy(self):
        netsh = FakeNetsh({win.RULE_PREFIX + str(p): 4 for p in (9, 7, 67, 68, 45678)})
        removed = remove(netsh)
        self.assertEqual(len(removed), 5, removed)
        self.assertEqual(netsh.total(), 0, "rules survived uninstall: %s" % netsh.copies)

    def test_uninstall_on_a_clean_machine_claims_nothing(self):
        netsh = FakeNetsh()
        self.assertEqual(remove(netsh), [])
        self.assertEqual(netsh.total(), 0)

    def test_a_rule_named_like_ours_but_foreign_is_still_ours_to_clear(self):
        # names are namespaced by RULE_PREFIX; anything carrying one of our names is by definition
        # ours (v1 or an older v2 left it), so uninstall must not need to recognise the port list
        netsh = FakeNetsh({win.RULE_PREFIX + "9": 3})
        self.assertEqual(remove(netsh), [win.RULE_PREFIX + "9"])
        self.assertEqual(netsh.total(), 0)


if __name__ == "__main__":
    unittest.main()