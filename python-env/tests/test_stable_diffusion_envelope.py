import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml
from jinja2 import FileSystemLoader

from models import Notification, Text2Image
from runner import main
from yaml_compiler import PROJECT_ROOT, jinja_env, parse


class StableDiffusionEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.env = jinja_env.overlay(
            loader=FileSystemLoader(PROJECT_ROOT / "templates")
        )
        self.task = Text2Image(
            name="T2I",
            location="desktop",
            type="text2image",
            model="illustrious",
            user_input='A "sunny" beach: Greece\nwith waves',
            width=1024,
            height=768,
            image_seed=123,
            out="/tmp/beach.png",
        )

    def compile(self, **values):
        return yaml.safe_load(
            self.env.get_template(
                "envelopes/stable-diffusion_desktop.yaml.j2"
            ).render(**values)
        )

    def notification(self, **overrides):
        data = self.compile(deliveries=[{"type": "notification", **overrides}])
        return Notification(**data["deliveries"][0])

    def context(self, success=True, output_paths=None):
        result = self.task.build_result(
            success=success, error=None if success else "GPU out of memory"
        )
        result.duration = 2.5
        if output_paths is not None:
            result.output_paths = output_paths
        return {
            "definition": {"name": self.task.name},
            "task": self.task.model_dump(exclude={"deliveries"}),
            "result": result.model_dump(),
        }

    def test_compile_preserves_runtime_expressions_without_result(self):
        delivery = self.notification()
        for value in (
            delivery.title,
            delivery.body,
            delivery.output,
            delivery.attachment,
            *delivery.tags[-2:],
        ):
            self.assertIn("result.success", value)

    def test_success_defaults_render_task_and_result(self):
        rendered = self.notification().render(
            self.context(output_paths=[Path("/tmp/actual-image.png")])
        )
        self.assertEqual(rendered.title, "[SUCCESS] T2I (2.5s)")
        self.assertEqual(
            rendered.output,
            "https://mesh-notifications.taile72000.ts.net/attachments/beach.png",
        )
        self.assertEqual(rendered.attachment, "/tmp/actual-image.png")
        self.assertEqual(
            rendered.tags, ["video", "desktop", "white_check_mark", "sparkles"]
        )
        self.assertIn("### Image Rendered (2.5s)\n", rendered.body)
        self.assertIn("(1024x768)", rendered.body)
        self.assertIn("`illustrious` (40 steps, Seed: 123)", rendered.body)
        self.assertIn(self.task.prompt, rendered.body)
        self.assertNotIn("Failed", rendered.body)
        self.assertNotIn("{{", rendered.model_dump_json())
        self.assertNotIn("{%", rendered.model_dump_json())

    def test_success_without_output_paths_falls_back_to_task_output(self):
        rendered = self.notification().render(self.context(output_paths=[]))
        self.assertEqual(rendered.attachment, "/tmp/beach.png")

    def test_failure_sends_error_body_without_uploading_missing_output(self):
        rendered = self.notification().render(self.context(success=False))
        self.assertEqual(rendered.title, "[FAILED] T2I (2.5s)")
        self.assertEqual(rendered.output, "")
        self.assertEqual(rendered.attachment, "")
        self.assertEqual(
            rendered.tags, ["video", "desktop", "rotating_light", "x"]
        )
        self.assertIn("### Image Generation Failed (2.5s)\n", rendered.body)
        self.assertIn("`GPU out of memory`", rendered.body)
        self.assertIn(self.task.prompt, rendered.body)
        self.assertIn("`/tmp/beach.png`", rendered.body)
        self.assertNotIn("Watch / Stream", rendered.body)
        with patch("models.run") as run_command, redirect_stdout(io.StringIO()):
            rendered.deliver()
        command = run_command.call_args.args[0]
        self.assertNotIn("--data-binary", command)
        self.assertIn(rendered.body, command)
        self.assertFalse(any(arg.startswith("Click:") for arg in command))

    def test_explicit_notification_overrides_are_preserved(self):
        overrides = {
            "endpoint": "https://example.com/notifications",
            "title": 'Custom "title": {{ definition.name }}',
            "body": "Custom body\n{{ result.error }}",
            "output": "https://example.com/result",
            "attachment": "/tmp/custom.png",
            "tags": ["custom"],
            "destination": "custom-device",
            "priority": 5,
            "markdown": False,
        }
        delivery = self.notification(**overrides)
        self.assertEqual(delivery.model_dump(), {"type": "notification", **overrides})
        rendered = delivery.render(self.context(success=False))
        self.assertEqual(rendered.title, 'Custom "title": T2I')
        self.assertEqual(rendered.body, "Custom body\nGPU out of memory")

    def test_explicit_empty_fields_disable_defaults(self):
        overrides = {
            "title": "",
            "body": "",
            "output": "",
            "attachment": "",
            "tags": [],
        }
        rendered = self.notification(**overrides).render(self.context())
        for field, expected in overrides.items():
            self.assertEqual(getattr(rendered, field), expected)

    def test_omitted_or_empty_deliveries_do_not_send_notifications(self):
        for values in ({}, {"deliveries": []}):
            with self.subTest(values=values):
                self.assertEqual(self.compile(**values)["deliveries"], [])

    def test_other_delivery_types_keep_their_values(self):
        deliveries = [
            {
                "type": "email",
                "to": "test@example.com",
                "subject": "{{ definition.name }}",
                "body": "Custom email",
                "attachments": [],
            },
            {"type": "download", "path": "/tmp/downloads"},
        ]
        self.assertEqual(self.compile(deliveries=deliveries)["deliveries"], deliveries)

    def test_t2i_definition_compiles_with_defaults_and_quoted_inputs(self):
        with patch("yaml_compiler.jinja_env", self.env):
            data = parse(
                PROJECT_ROOT / "definitions/tasks/t2i.yaml",
                user_input=self.task.prompt,
            )
        self.assertNotIn("instance", data)
        self.assertEqual(data["task"]["user_input"], self.task.prompt)
        self.assertIn("result.success", data["deliveries"][0]["title"])
        self.assertEqual(data["deliveries"][0]["destination"], "laptop-adrien")

    def test_runner_renders_defaults_after_task_success_or_failure(self):
        for success in (True, False):
            with self.subTest(success=success):
                with (
                    patch("yaml_compiler.jinja_env", self.env),
                    patch(
                        "sys.argv",
                        ["task", str(PROJECT_ROOT / "definitions/tasks/t2i.yaml")],
                    ),
                    patch.object(Text2Image, "execute") as execute,
                    patch.object(Notification, "deliver", autospec=True) as deliver,
                    redirect_stdout(io.StringIO()),
                ):
                    if success:
                        execute.return_value = self.task.build_result()
                    else:
                        execute.side_effect = RuntimeError("GPU out of memory")
                    main()
                deliver.assert_called_once()
                rendered = deliver.call_args.args[0]
                self.assertIn("[SUCCESS]" if success else "[FAILED]", rendered.title)
                if not success:
                    self.assertEqual(rendered.attachment, "")
                    self.assertIn("GPU out of memory", rendered.body)


if __name__ == "__main__":
    unittest.main()
