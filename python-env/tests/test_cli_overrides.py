import unittest

from yaml_compiler import parse_cli_overrides


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
