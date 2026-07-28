import ast
from pathlib import Path
import re

from infiniwolf.config import GenerationQuality
import unittest

from infiniwolf import __version__


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = ("README.md", "DESIGN.md", "GENERATION_FLOW.md")


class DocumentationTests(unittest.TestCase):
    def test_readme_has_exact_release_version_and_unicode_credit_footer(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertEqual(__version__, "2.0.1")
        self.assertIn(
            "python3 packaging/make_release.py --platform linux --version 2.0.1",
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


    def test_relative_import_graph_is_acyclic(self):
        """The documented package import graph has no relative-import cycle."""
        modules = {
            path.stem: path for path in (ROOT / "infiniwolf").glob("*.py")
        }
        graph = {name: set() for name in modules}
        for name, path in modules.items():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                if node.level != 1 or not node.module:
                    continue
                target = node.module.split(".", 1)[0]
                if target in modules:
                    graph[name].add(target)

        visiting, visited = set(), set()

        def visit(module):
            self.assertNotIn(module, visiting, f"relative import cycle at {module}")
            if module in visited:
                return
            visiting.add(module)
            for dependency in graph[module]:
                visit(dependency)
            visiting.remove(module)
            visited.add(module)

        for module in graph:
            visit(module)

    def test_relative_imports_are_top_level(self):
        """Relative imports stay eager rather than hiding in functions or file tails."""
        for path in (ROOT / "infiniwolf").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            first_definition = min(
                (node.lineno for node in tree.body
                 if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))),
                default=float("inf"))
            parents = {id(child): parent for parent in ast.walk(tree)
                       for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                self.assertIsInstance(
                    parents[id(node)], ast.Module,
                    f"function-local relative import in {path.name}:{node.lineno}")
                self.assertLess(
                    node.lineno, first_definition,
                    f"bottom-of-file relative import in {path.name}:{node.lineno}")

    def test_generation_quality_pool_sizes(self):
        """Candidate-pool sizes documented for the three quality tiers are stable."""
        self.assertEqual(GenerationQuality.FAST.pool_size, 3)
        self.assertEqual(GenerationQuality.BALANCED.pool_size, 5)
        self.assertEqual(GenerationQuality.THOROUGH.pool_size, 8)


if __name__ == "__main__":
    unittest.main()
