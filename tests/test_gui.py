import ast
import inspect
import textwrap
import unittest

from infiniwolf.gui import App


class GuiLayoutTests(unittest.TestCase):
    def test_packed_action_buttons_belong_to_action_frame(self):
        """Tk cannot mix pack and grid among children of the same parent."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(App._build)))
        parents = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            call = node.value
            if (not isinstance(target, ast.Attribute)
                    or not isinstance(target.value, ast.Name)
                    or target.value.id != "self"
                    or not isinstance(call, ast.Call)
                    or not isinstance(call.func, ast.Attribute)
                    or call.func.attr != "Button"
                    or not call.args):
                continue
            parent = call.args[0]
            parents[target.attr] = parent.id if isinstance(parent, ast.Name) else None

        button_names = (
            "generate_button", "cancel_button", "play_button", "view_button")
        self.assertEqual(
            {name: parents.get(name) for name in button_names},
            {name: "actions" for name in button_names},
        )


if __name__ == "__main__":
    unittest.main()
