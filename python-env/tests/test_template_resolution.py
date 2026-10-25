import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from jinja2 import TemplateNotFound

from yaml_compiler import parse


class TemplateResolutionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workflow = Path(directory.name) / "workflow.yaml"

    def test_bare_and_qualified_envelope_names_compile_identically(self):
        for name in ("base.yaml.j2", "stable-diffusion_desktop.yaml.j2"):
            with self.subTest(template=name):
                results = []
                for template in (f"envelopes/{name}", name):
                    self.workflow.write_text(
                        yaml.safe_dump(
                            {
                                "template": template,
                                "values": {
                                    "task_definition": (
                                        "tasks/stable-diffusion/sdxl/t2i_simple.yaml.j2"
                                    ),
                                    "deliveries": [{"type": "notification"}],
                                },
                            }
                        )
                    )
                    results.append(parse(self.workflow, user_input="test prompt"))
                self.assertEqual(results[0], results[1])
                self.assertEqual(results[1]["task"]["user_input"], "test prompt")
                self.assertEqual(results[1]["deliveries"][0]["type"], "notification")
                self.assertNotIn("instance", results[1])

    def test_task_names_resolve_relative_to_tasks_or_templates(self):
        name = "stable-diffusion/flux/t2i_simple.yaml.j2"
        for envelope in ("base.yaml.j2", "stable-diffusion_desktop.yaml.j2"):
            with self.subTest(envelope=envelope):
                results = []
                for task_definition in (f"tasks/{name}", name):
                    self.workflow.write_text(
                        yaml.safe_dump(
                            {
                                "template": envelope,
                                "values": {
                                    "task_definition": task_definition,
                                    "model": "flux_fp8",
                                    "user_input": "A woman standing on a pier in a marina",
                                    "steps": 12,
                                },
                            }
                        )
                    )
                    results.append(parse(self.workflow))
                self.assertEqual(results[0], results[1])
                self.assertEqual(results[1]["task"]["model"], "flux_fp8")
                self.assertEqual(
                    results[1]["task"]["user_input"],
                    "A woman standing on a pier in a marina",
                )
                self.assertEqual(results[1]["task"]["steps"], 12)

    def test_omitted_template_uses_base_envelope(self):
        self.workflow.write_text("values:\n  definition:\n    name: Default envelope\n")
        data = parse(self.workflow)
        self.assertEqual(data["definition"]["name"], "Default envelope")
        self.assertEqual(data["definition"]["type"], "text2image")
        self.assertEqual(data["deliveries"], [])
        self.assertNotIn("instance", data)

    def test_missing_template_still_raises(self):
        self.workflow.write_text("template: nonexistent-envelope.yaml.j2\n")
        with self.assertRaises(TemplateNotFound):
            parse(self.workflow)


if __name__ == "__main__":
    unittest.main()
