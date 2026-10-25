import unittest
from unittest.mock import patch

import yaml
from jinja2 import FileSystemLoader

from yaml_compiler import PROJECT_ROOT, jinja_env, parse


class YamlQuoteTests(unittest.TestCase):
    def setUp(self):
        self.env = jinja_env.overlay(
            loader=FileSystemLoader(PROJECT_ROOT / "templates")
        )

    def test_yaml_quote_round_trips_values(self):
        template = self.env.from_string("value: {{ value | yamlQuote }}")
        values = [
            "",
            "  padded  ",
            'A "sunny" beach: Greece\nwith waves # sunset',
            "it's a beach",
            r"C:\images\output.png",
            "line\r\nnext\tcolumn\x00",
            "control\x01\b\f\x7f\u0085characters",
            "caf\u00e9 \U0001f305\u2028next",
            "a long prompt " * 100,
            "null",
            "true",
            "yes",
            "0123",
            "2026-10-24",
            "[one, two]",
            "{key: value}",
            "{{ result.success }}",
            None,
            True,
            42,
            0.5,
            ["one", None],
            {"key": "value"},
        ]
        for value in values:
            with self.subTest(value=value):
                rendered = template.render(value=value)
                self.assertNotIn("\n", rendered)
                actual = yaml.safe_load(rendered)["value"]
                self.assertEqual(actual, value)
                self.assertIs(type(actual), type(value))

    def test_all_stable_diffusion_templates_compile_through_envelopes(self):
        task_templates = [
            name
            for name in self.env.list_templates()
            if name.startswith("tasks/stable-diffusion/")
            and name.endswith(".yaml.j2")
        ]
        self.assertTrue(task_templates)
        for envelope in (
            "envelopes/base.yaml.j2",
            "envelopes/stable-diffusion_desktop.yaml.j2",
        ):
            template = self.env.get_template(envelope)
            for task_template in task_templates:
                with self.subTest(envelope=envelope, task=task_template):
                    data = yaml.safe_load(
                        template.render(task_definition=task_template)
                    )
                    self.assertIsInstance(data["task"]["model"], str)
                    self.assertEqual(data["task"]["user_input"], "")
                    self.assertEqual(data["task"]["negative_input"], "")
                    self.assertIsInstance(data["task"]["out"], str)

    def test_parse_flux_i2i_preserves_quoted_overrides(self):
        values = {
            "model": "null",
            "user_input": 'A "sunny" beach: Greece\nwith waves # sunset',
            "negative_input": "blurry: yes\nlow quality",
            "init_image": [r"C:\images\input.png", "image: two.png"],
            "loras": [{"name": "artist's style: v2", "scale": 0.75}],
            "out": "/tmp/output: final.png",
        }
        for variant in ("simple", "adv"):
            with self.subTest(variant=variant):
                with patch("yaml_compiler.jinja_env", self.env):
                    data = parse(
                        PROJECT_ROOT / "definitions/tasks/t2i.yaml",
                        task_definition=(
                            f"tasks/stable-diffusion/flux/i2i_{variant}.yaml.j2"
                        ),
                        **values,
                    )
                for key, value in values.items():
                    self.assertEqual(data["task"][key], value)
                self.assertNotIn("instance", data)
                self.assertIn("result.success", data["deliveries"][0]["title"])


if __name__ == "__main__":
    unittest.main()
