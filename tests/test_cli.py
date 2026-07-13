from pathlib import Path
import tempfile
import unittest

from infiniwolf.cli import main, parser


class CliTests(unittest.TestCase):
    def test_parser_accepts_all_controls(self):
        args = parser().parse_args(["--seed", "test", "--guard-density", "5",
                                    "--layout-complexity", "1", "--atmosphere", "2",
                                    "--theme-bias", "grand-halls"])
        self.assertEqual(args.seed, "test")
        self.assertEqual(args.guard_density, 5)
        self.assertEqual(args.layout_complexity, 1)
        self.assertEqual(args.atmosphere, 2)
        self.assertEqual(args.theme_bias, "grand-halls")

    def test_cli_generates_package(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "campaign.pk3"
            self.assertEqual(main(["--seed", "99", "--output", str(output)]), 0)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
