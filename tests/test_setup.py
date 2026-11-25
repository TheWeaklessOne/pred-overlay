"""The setup exe (packaging/installer.iss) and the app agree on where things are."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from predoverlay import install, updater
from predoverlay import settings as settings_module

ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")


def setting(name):
    found = re.search(rf"^{name}=(.*)$", ISS, re.MULTILINE)
    assert found, name
    return found.group(1).strip()


class FakeRegistry:
    """The parts of winreg that sync_setup_entry uses, over one key."""

    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = object(), 1, 2, 1

    def __init__(self, values=None):
        self.values = values
        self.opened = []

    def OpenKey(self, root, path, reserved, access):
        self.opened.append(path)
        if self.values is None:
            raise FileNotFoundError(path)
        registry = self

        class Key:
            def __enter__(self):
                return registry

            def __exit__(self, *exc):
                return False

        return Key()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value


class InstallerScript(unittest.TestCase):
    def test_installs_where_the_updater_swaps(self):
        self.assertEqual(setting("DefaultDirName"), r"{localappdata}\Programs\PredOverlay")
        self.assertEqual(setting("DisableDirPage"), "yes")
        self.assertEqual(setting("PrivilegesRequired"), "lowest")

    def test_uninstaller_stays_out_of_the_program_folder(self):
        # The updater swaps only a folder that holds nothing but the app.
        where = setting("UninstallFilesDir")
        self.assertNotIn("{app}", where)
        self.assertEqual(where, setting("DefaultDirName") + ".uninstall")
        self.assertEqual({"PredOverlay.exe", "PredOverlayCLI.exe", "_internal"}, updater.APP_FILES)

    def test_app_id_matches_the_apps_entry_the_app_keeps_current(self):
        app_id = "{" + setting("AppId").lstrip("{")  # "{{" escapes the brace in Inno Setup
        self.assertTrue(install.SETUP_KEY.endswith("\\" + app_id + "_is1"), app_id)

    def test_commands_run_through_the_windowed_exe(self):
        # PredOverlayCLI.exe is a console exe: run by the setup it could open a window.
        commands = re.findall(r'^Filename: "([^"]+)"', ISS, re.MULTILINE)
        self.assertTrue(commands)
        self.assertTrue(all(c.endswith(r"\PredOverlay.exe") for c in commands), commands)
        runs = re.findall(r"\bExec\(([^,]+),", ISS.split("[Code]", 1)[1])
        self.assertTrue(runs)
        self.assertEqual(set(runs), {"AppExe()"})
        self.assertIn(r"'{app}\PredOverlay.exe'", ISS)


class SetupEntry(unittest.TestCase):
    APP = r"D:\Local\Programs\PredOverlay"

    def test_updates_the_version_after_an_update(self):
        fake = FakeRegistry({"InstallLocation": self.APP + "\\", "DisplayVersion": "0.5.25"})
        self.assertTrue(install.sync_setup_entry("0.5.26", self.APP, fake))
        self.assertEqual(fake.values["DisplayVersion"], "0.5.26")
        self.assertEqual(fake.opened, [install.SETUP_KEY])

    def test_leaves_a_current_entry_alone(self):
        fake = FakeRegistry({"InstallLocation": self.APP, "DisplayVersion": "0.5.26"})
        self.assertFalse(install.sync_setup_entry("0.5.26", self.APP, fake))

    def test_leaves_an_entry_for_another_folder_alone(self):
        fake = FakeRegistry(
            {"InstallLocation": r"D:\Elsewhere\PredOverlay", "DisplayVersion": "0.5.25"}
        )
        self.assertFalse(install.sync_setup_entry("0.5.26", self.APP, fake))
        self.assertEqual(fake.values["DisplayVersion"], "0.5.25")

    def test_zip_install_has_no_entry(self):
        self.assertFalse(install.sync_setup_entry("0.5.26", self.APP, FakeRegistry(None)))

    def test_entry_without_values(self):
        self.assertFalse(install.sync_setup_entry("0.5.26", self.APP, FakeRegistry({})))

    def test_values_of_another_type_do_not_stop_the_start(self):
        fake = FakeRegistry({"InstallLocation": 7, "DisplayVersion": "0.5.25"})
        self.assertFalse(install.sync_setup_entry("0.5.26", self.APP, fake))

    def test_from_source_nothing_is_touched(self):
        self.assertFalse(install.sync_setup_entry("0.5.26"))  # not a built app: no registry


class Reinstall(unittest.TestCase):
    def test_uninstall_lets_the_next_install_set_itself_up_again(self):
        # Uninstalled with the settings kept, then installed again: without this the
        # first start found "integrated" and made no shortcuts and no autostart.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            values = settings_module.load(path)
            values["integrated"] = True
            settings_module.save(values, path)
            with (
                mock.patch.object(install.autostart, "supported", return_value=False),
                mock.patch.object(install, "supported", return_value=False),
            ):
                install.uninstall(path)
            self.assertFalse(settings_module.load(path)["integrated"])

    def test_uninstall_makes_no_settings_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            with mock.patch.object(install.autostart, "supported", return_value=False):
                install.uninstall(path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
