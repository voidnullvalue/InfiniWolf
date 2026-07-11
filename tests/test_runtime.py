from pathlib import Path
import tempfile
import unittest

from randomwolf.runtime import AppSettings, launch_command, load_settings, save_settings, validate_settings


WL6_FILES = ("AUDIOHED.WL6", "AUDIOT.WL6", "GAMEMAPS.WL6", "MAPHEAD.WL6",
             "VGADICT.WL6", "VGAGRAPH.WL6", "VGAHEAD.WL6", "VSWAP.WL6")


class RuntimeTests(unittest.TestCase):
    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            expected = AppSettings("/games/ecwolf", "/games/data", "/games/randomwolf.pk3")
            save_settings(expected, path)
            self.assertEqual(load_settings(path), expected)

    def test_validation_and_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "ecwolf"
            executable.touch()
            executable.chmod(0o755)
            data = root / "data"; data.mkdir()
            for name in WL6_FILES: (data / name).touch()
            settings = AppSettings(str(executable), str(data), str(root / "randomwolf.pk3"))
            self.assertEqual(validate_settings(settings), [])
            command = launch_command(settings)
            self.assertEqual(command[:4], [str(executable), "--data", "wl6", "--file"])


if __name__ == "__main__":
    unittest.main()
