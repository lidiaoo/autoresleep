"""Every operator-facing string goes through the locale table, and every call to it works.

Two failure modes are guarded here, both of them real:

  * a call site using a **keyword argument** - `_loc._("key", done=...)` - while `_()` takes
    positional args only. That raises `TypeError: _() got an unexpected keyword argument 'done'`,
    and because it happens inside the ELEVATED child (the firewall step), it read as "the
    privileged step did not finish" with the real cause only in the progress file. A dry run and
    the unit suite both passed: the code path only runs with privileges raised.
  * a key referenced in cli.py but **missing from a table** (or one using `%d` where the call
    passes a string). `_()` looks the key up at call time, so a missing key or a bad format spec
    is a `KeyError`/`TypeError` at the moment the sentence is printed - again, only on the path
    that prints it.

These are source-level checks on purpose: they cannot be fooled by a code path a test never runs.
"""
import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import cli, locale  # noqa: E402


def _cli_source() -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "autoresleep" / "cli.py").read_text(
        encoding="utf-8")


def _referenced_keys(src: str):
    """Every `_loc._("some.key"` literal in the source, in order."""
    return re.findall(r'_loc\._\(\s*"([a-z0-9._]+)"', src)


def _placeholder_order(template: str):
    spec = re.compile(r"%[-+\s#0]*\d*(?:\.\d+)?([sd])")
    return [m.group(1) for m in spec.finditer(template)]


class TestEveryLocaleCallIsAWorkingCall(unittest.TestCase):
    def test_no_call_site_passes_a_keyword_argument(self):
        # _() is positional-only. A kwarg call raises TypeError only when that line runs.
        src = _cli_source()
        offenders = re.findall(r'_loc\._\(\s*"[a-z0-9._]+"\s*,\s*([a-z_]+)\s*=', src)
        self.assertEqual(offenders, [],
                         "these _loc._() calls pass keyword args; _() takes positional args only")

    def test_every_referenced_key_exists_in_both_tables(self):
        src = _cli_source()
        keys = set(_referenced_keys(src))
        self.assertGreater(len(keys), 50, "expected the install/uninstall flow to be localised")
        for key in sorted(keys):
            self.assertIn(key, locale.EN, "referenced in cli.py but missing from the EN table")
            self.assertIn(key, locale.ZH, "referenced in cli.py but missing from the ZH table")

    def test_the_two_tables_have_the_same_keys(self):
        # a key in one table but not the other means one language silently falls back mid-sentence
        only_en = sorted(set(locale.EN) - set(locale.ZH))
        only_zh = sorted(set(locale.ZH) - set(locale.EN))
        self.assertEqual(only_en, [], "these keys exist in EN only")
        self.assertEqual(only_zh, [], "these keys exist in ZH only")

    def test_both_tables_use_the_same_placeholder_order(self):
        # The call site passes ONE argument order; if EN and ZH disagree on where %s / %d sit,
        # one language works and the other raises "TypeError: %d format: ... not str" the moment
        # it is printed. This is the bug that broke the elevated firewall line.
        for key in sorted(set(locale.EN) & set(locale.ZH)):
            self.assertEqual(_placeholder_order(locale.EN[key]), _placeholder_order(locale.ZH[key]),
                             "EN/ZH placeholder order differs for key %r" % key)

    def test_every_key_formats_in_every_language(self):
        # actually switch the active table and format, so a bad spec or missing arg raises here
        try:
            for lang, name in (("en", "EN"), ("zh", "ZH")):
                locale.set_language(lang)
                table = locale.EN if lang == "en" else locale.ZH
                for key, template in table.items():
                    args = [7 if kind == "d" else "x" for kind in _placeholder_order(template)]
                    try:
                        out = locale._(key, *args)
                    except Exception as exc:   # noqa: BLE001 - the point is to name the key
                        self.fail("%s[%r] failed to format: %s" % (name, key, exc))
                    self.assertIsInstance(out, str)
        finally:
            locale.set_language("zh")   # the default the CLI starts in

    def test_switching_language_changes_a_known_sentence(self):
        try:
            locale.set_language("en")
            en = locale._("menu.5")
            locale.set_language("zh")
            self.assertNotEqual(en, locale._("menu.5"))
        finally:
            locale.set_language("zh")


class _FakeProgress:
    def __init__(self):
        self.lines = []

    def start(self, what):
        self.lines.append("start %s" % what)

    def step(self, what):
        self.lines.append("step %s" % what)

    def say(self, what):
        self.lines.append("say %s" % what)

    def done(self, rc):
        self.lines.append("done rc=%d" % rc)


class _FakeSystem:
    firewall_supported = True

    def __init__(self):
        self.applied = None

    def firewall_apply(self, ports, marker_port, program):
        self.applied = (list(ports), marker_port, program)
        return ["AutoReSleep-WoL-UDP-9"]


class _FakeConfig:
    repeat_minutes = 2
    ports = [9, 7]
    marker_port = 45678

    def validate(self):
        return self


class TestTheElevatedHalfPrintsInBothLanguages(unittest.TestCase):
    """The exact path that broke: the elevated child reaching its firewall line and printing it.

    The firewall step is the last thing the privileged half does, and it is the line that raised
    `TypeError: _() got an unexpected keyword argument 'done'` on the real machine - only under
    elevation, so no dry run and no unit test had ever executed it.
    """

    def _run(self, lang):
        locale.set_language(lang)
        args = mock.Mock(platform=None, config=None, no_sensor=False, no_firewall=False,
                         progress=str(Path(tempfile.mkdtemp()) / "elevated-install.progress"))
        progress = _FakeProgress()
        sysinfo = _FakeSystem()
        buf = io.StringIO()
        with mock.patch.object(cli, "_attach_progress", return_value=progress), \
                mock.patch.object(cli, "_stop_stale_listeners"), \
                mock.patch.object(cli, "_load_config", return_value=_FakeConfig()), \
                mock.patch.object(cli, "_system", return_value=sysinfo), \
                mock.patch.object(cli, "artifacts_for", return_value=[]), \
                mock.patch.object(cli, "_firewall_program", return_value="pythonw.exe"), \
                redirect_stdout(buf):
            rc = cli._elevated_install(args)
        return rc, buf.getvalue(), sysinfo

    def test_the_privileged_half_finishes_in_english(self):
        try:
            rc, out, sysinfo = self._run("en")
            self.assertEqual(rc, 0, "the elevated half must finish; it raised instead")
            self.assertEqual(sysinfo.applied[0], [9, 7])
            self.assertIn("firewall:", out)      # detail.firewall in the EN table
        finally:
            locale.set_language("zh")

    def test_the_privileged_half_finishes_in_chinese(self):
        try:
            rc, out, _ = self._run("zh")
            self.assertEqual(rc, 0, "the elevated half must finish; it raised instead")
            self.assertIn("防火墙", out)      # detail.firewall in the ZH table
        finally:
            locale.set_language("zh")


if __name__ == "__main__":
    unittest.main()