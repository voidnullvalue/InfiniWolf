from pathlib import Path
import re
import unittest

from infiniwolf import __version__


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = ("README.md", "DESIGN.md", "GENERATION_FLOW.md")


class DocumentationTests(unittest.TestCase):
    def test_readme_has_exact_release_version_and_unicode_credit_footer(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertEqual(__version__, "1.9.1")
        self.assertIn(
            "python3 packaging/make_release.py --platform linux --version 1.9.1",
            readme)
        self.assertTrue(
            readme.rstrip().endswith(
                "## Credits\n\nSeñor Frijole — testing and map-design feedback."),
            "README credit footer lost its exact Unicode spelling or punctuation")

    def test_public_docs_match_current_release_and_layout_rules(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        flow = (ROOT / "GENERATION_FLOW.md").read_text(encoding="utf-8")
        release_builder = (ROOT / "packaging" / "make_release.py").read_text(
            encoding="utf-8")

        self.assertIn(f"--version {__version__}", readme)
        self.assertIn("16/18/20/22/24", readme)
        self.assertIn("90% of the authored progression spine", design)
        self.assertIn("≥90% of the progression spine", flow)
        self.assertNotIn("≥55% of rooms", flow)
        self.assertIn("View Maps opens a top-down viewer", release_builder)
        self.assertIn("one of three believable", readme)
        self.assertNotIn("one of four believable", readme)
        self.assertIn("plaster pushwalls remain plain", design)
        self.assertNotIn("weighted inert façade", flow)
        self.assertFalse((ROOT / "ROADMAP.md").exists())

    def test_public_markdown_relative_links_exist(self):
        link_pattern = re.compile(r"\[[^]]*\]\(([^)]+)\)")
        for name in PUBLIC_DOCS:
            path = ROOT / name
            for target in link_pattern.findall(path.read_text(encoding="utf-8")):
                if (target.startswith(("http://", "https://", "mailto:", "#"))
                        or "{" in target):
                    continue
                relative = target.split("#", 1)[0]
                if relative:
                    self.assertTrue(
                        (path.parent / relative).exists(),
                        f"{name} links to missing public path {relative}")


if __name__ == "__main__":
    unittest.main()
