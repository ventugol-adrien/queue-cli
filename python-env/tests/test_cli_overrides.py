import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch

from jinja2 import ChoiceLoader, FileSystemLoader
import yaml

from models import BaseDelivery, RemoteInstanceRequest, ScalewayInstance
from yaml_compiler import jinja_env, parse_cli_overrides


class InstanceTemplateTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).parent / "fixtures"
        self.jinja_env = jinja_env.overlay(
            loader=ChoiceLoader(
                [
                    FileSystemLoader([fixtures, fixtures / "instances"]),
                    jinja_env.loader,
                ]
            )
        )

    def render(self, **values):
        context = {"definition": {"name": "test", "location": "remote"}}
        return yaml.safe_load(
            self.jinja_env.get_template("base.yaml.j2").render(**(context | values))
        )

    def test_missing_instance_uses_request_defaults(self):
        data = self.render()
        self.assertEqual(
            RemoteInstanceRequest(**data["instance"]), RemoteInstanceRequest()
        )
        self.assertEqual(data["deliveries"], [])
        self.assertEqual(BaseDelivery.from_list(data["deliveries"]), [])

    def test_ranges_and_null_bounds_survive_rendering(self):
        instance = {
            "gpu_memory_rng": [640, None],
            "vcpus_rng": [128, 128],
            "ram_rng": [None, 960],
            "storage_rng": [None, None],
            "bandwidth_rng": [20000, None],
            "metadata": {"job": "test"},
        }
        for preset in (None, "instances/render-s.yaml"):
            with self.subTest(preset=preset):
                data = self.render(instance=instance, instance_definition=preset)
                request = RemoteInstanceRequest(**data["instance"])
                for field, bounds in instance.items():
                    self.assertEqual(
                        getattr(request, field),
                        tuple(bounds) if field.endswith("_rng") else bounds,
                    )

    @patch("models.run")
    def test_inline_h100_request_completes_concrete_instance(self, run_command):
        run_command.return_value = Mock(
            stdout=json.dumps(
                [
                    {
                        "name": "H100-SXM-8-80G",
                        "availability": "scarce",
                        "gpu": 8,
                        "cpu": 128,
                        "ram": 960 * 1024**3,
                        "local_volume_max_size": 0,
                        "bandwidth": 20000 * 10**6,
                        "hourly_price": {"units": 25, "nanos": 330800000},
                    }
                ]
            )
        )
        data = self.render(
            instance={
                "type": "H100-SXM-8-80G",
                "image": "custom-image",
                "timeout_minutes": 12,
                "save": True,
                "tags": ["worker's job"],
                "metadata": {"job": "test"},
            },
        )
        instance = RemoteInstanceRequest.from_dict(data["instance"])()
        self.assertIsInstance(instance, ScalewayInstance)
        self.assertEqual(instance.type, "H100-SXM-8-80G")
        self.assertEqual(instance.gpu_memory, 640)
        self.assertEqual(instance.vcpus, 128)
        self.assertEqual(instance.ram, 960)
        self.assertEqual(instance.storage, 0)
        self.assertEqual(instance.bandwidth, 20000)
        self.assertEqual(instance.image, "custom-image")
        self.assertEqual(instance.timeout_minutes, 12)
        self.assertTrue(instance.save_on_completion)
        self.assertEqual(instance.tags, ["worker's job"])
        self.assertEqual(instance.metadata, {"job": "test"})

    def test_desktop_omits_instance(self):
        self.assertNotIn("instance", self.render(definition={"location": "desktop"}))

    def test_plain_yaml_instance_definition(self):
        data = self.render(instance_definition="instances/render-s.yaml")
        request = RemoteInstanceRequest(**data["instance"])
        self.assertEqual(request.type, "RENDER-S")
        self.assertEqual(request.image, "base")
        self.assertEqual(request.zone, "fr-par-2")
        self.assertEqual(request.timeout_minutes, 5)
        self.assertFalse(request.save_on_completion)
        self.assertEqual(request.tags, ["ephemeral", "worker"])

    def test_instance_definition_accepts_unprefixed_filename(self):
        short_name = self.render(instance_definition="render-s.yaml")
        prefixed_name = self.render(instance_definition="instances/render-s.yaml")
        self.assertEqual(short_name, prefixed_name)
        self.assertEqual(
            RemoteInstanceRequest(**short_name["instance"]).type, "RENDER-S"
        )


class ParseCliOverridesTests(unittest.TestCase):
    def test_prompt_flag_spellings(self):
        prompt = "A sun-lit mountain"
        for flag in ("--user-input", "--user_input"):
            for arguments in ([flag, prompt], [f"{flag}={prompt}"]):
                with self.subTest(arguments=arguments):
                    self.assertEqual(
                        parse_cli_overrides(arguments), {"user_input": prompt}
                    )

    def test_nested_keys_and_typed_values(self):
        self.assertEqual(
            parse_cli_overrides(
                [
                    "--task-options.cfg-scale=2.5",
                    "--hi-res-fix",
                    "--image-seed",
                    "42",
                    "--normalize-embeddings",
                    "false",
                ]
            ),
            {
                "task_options": {"cfg_scale": 2.5},
                "hi_res_fix": True,
                "image_seed": 42,
                "normalize_embeddings": False,
            },
        )

    def test_last_spelling_wins(self):
        self.assertEqual(
            parse_cli_overrides(
                ["--user_input", "original", "--user-input", "replacement"]
            ),
            {"user_input": "replacement"},
        )


if __name__ == "__main__":
    unittest.main()
