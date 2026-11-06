import io
import os
import shlex
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from jinja2 import FileSystemLoader
from pydantic import ValidationError

from models import IngestJob, Task
from runner import main
from utils import resolve_bin
from yaml_compiler import PROJECT_ROOT, jinja_env


class IngestJobTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="queue tests ")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.bin_dir = self.home / "bin"
        self.bin_dir.mkdir()
        self.functions = self.home / ".config" / "bash" / "functions"
        self.functions.mkdir(parents=True)
        self.result_file = self.home / "result"
        self.url_file = self.home / "url"
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        (self.bin_dir / "bash").symlink_to(bash)
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "PATH": str(self.bin_dir),
                "RESULT_FILE": str(self.result_file),
                "URL_FILE": str(self.url_file),
                "JOB_DB_INSERT": "original",
                "GEMINI_SECRET_PATH": "original",
                "BASH_ENV": "",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        home_patch = patch("utils.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.url = "https://example.test/job?q='quoted'&x=$(printf unsafe);#"
        self.bodies = {
            "ashby_job": 'export ASHBY_URL="$1"; printf "%s" "$1" > "$URL_FILE"',
            "scrape": 'printf "%s\\n" "${ASHBY_URL:-raw}"',
            "transform": 'while IFS= read -r line; do printf "transformed:%s\\n" "$line"; done',
            "ingest-job": (
                'IFS= read -r line; printf "%s|%s|%s" "$line" '
                '"$JOB_DB_INSERT" "$GEMINI_SECRET_PATH" > "$RESULT_FILE"; '
                """printf '{"id":"job-123"}\\n'"""
            ),
        }
        os.environ["BASH_ENV"] = str(
            self.function("ashby_job", self.bodies["ashby_job"])
        )
        for name, body in self.bodies.items():
            if name != "ashby_job":
                self.binary(name, body)

    def binary(self, name, body):
        path = self.bin_dir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return path

    def function(self, name, body):
        path = self.functions / f"{name}.sh"
        path.write_text(f"{name}() {{\n{body}\n}}\n")
        return path

    def task(self):
        return IngestJob(
            name="Ingest",
            location="desktop",
            type="ingest-job",
            platform="ashby",
            kind="job_description",
            url=self.url,
        )

    def test_resolves_binary_symlinks_and_prefers_path_over_function(self):
        binary = self.binary("scrape-real", "exit 0")
        (self.bin_dir / "scrape").unlink()
        (self.bin_dir / "scrape").symlink_to(binary)
        self.function("scrape", "exit 1")
        self.assertEqual(resolve_bin("scrape"), str(binary.resolve()))

    def test_does_not_resolve_function_source_files(self):
        (self.bin_dir / "scrape").unlink()
        self.function("scrape", "exit 1")
        with self.assertRaises(FileNotFoundError):
            resolve_bin("scrape")

    def test_missing_command_reports_path(self):
        with self.assertRaises(FileNotFoundError) as error:
            resolve_bin("missing-command")
        self.assertIn("PATH", str(error.exception))

    def test_function_directory_is_not_treated_as_source_file(self):
        (self.bin_dir / "scrape").unlink()
        (self.functions / "scrape.sh").mkdir()
        with self.assertRaises(FileNotFoundError):
            resolve_bin("scrape")

    def test_binary_and_function_pipeline_executes_and_preserves_arguments(self):
        with redirect_stdout(io.StringIO()):
            result = self.task()()
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.job_id, "job-123")
        self.assertEqual(result.lookup_command, "jds peek --id job-123")
        self.assertEqual(result.output_paths, [])
        self.assertEqual(self.url_file.read_text(), self.url)
        self.assertEqual(
            self.result_file.read_text(),
            f"transformed:{self.url}|true|original",
        )
        self.assertEqual(os.environ["JOB_DB_INSERT"], "original")
        self.assertEqual(os.environ["GEMINI_SECRET_PATH"], "original")

    def test_pipeline_runs_with_only_binaries(self):
        os.environ["BASH_ENV"] = ""
        for name, body in self.bodies.items():
            self.binary(name, body)
        with redirect_stdout(io.StringIO()):
            result = self.task()()
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.url_file.read_text(), self.url)
        self.assertEqual(
            self.result_file.read_text(), "transformed:raw|true|original"
        )

    def test_failure_in_any_stage_fails_task(self):
        for failed in self.bodies:
            with self.subTest(failed=failed):
                self.result_file.unlink(missing_ok=True)
                for name, body in self.bodies.items():
                    if name == "ashby_job":
                        self.function(name, "return 7" if name == failed else body)
                    else:
                        self.binary(name, "exit 7" if name == failed else body)
                with redirect_stdout(io.StringIO()):
                    result = self.task()()
                self.assertFalse(result.success)
                self.assertIn("non-zero exit status 7", result.error)
                if failed == "ashby_job":
                    self.assertFalse(self.result_file.exists())

    def test_preparation_is_invoked_directly_without_resolution(self):
        with (
            patch("models.resolve_bin", wraps=resolve_bin) as resolve,
            redirect_stdout(io.StringIO()),
        ):
            result = self.task()()
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            [call.args[0] for call in resolve.call_args_list],
            ["scrape", "transform", "ingest-job"],
        )

    def test_unset_secret_path_is_not_replaced_with_a_default(self):
        with (
            patch.dict(os.environ),
            patch("models.run") as run,
            redirect_stdout(io.StringIO()),
        ):
            os.environ.pop("GEMINI_SECRET_PATH", None)
            run.return_value.stdout = '{"id":"job-123"}'
            self.task().execute()
        self.assertNotIn("GEMINI_SECRET_PATH", run.call_args.kwargs["env"])

    def test_source_must_define_the_named_function(self):
        (self.functions / "ashby_job.sh").write_text(":\n")
        with redirect_stdout(io.StringIO()):
            result = self.task()()
        self.assertFalse(result.success)
        self.assertIn("non-zero exit status 127", result.error)
        self.assertFalse(self.result_file.exists())
        self.assertIsNone(result.job_id)
        self.assertEqual(result.lookup_command, "")

    def test_missing_command_becomes_failed_task_result(self):
        (self.bin_dir / "scrape").unlink()
        with redirect_stdout(io.StringIO()):
            result = self.task()()
        self.assertFalse(result.success)
        self.assertIn("scrape CLI was not found", result.error)

    def test_dry_run_does_not_source_or_execute_commands(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.task()(dry_run=True)
        self.assertTrue(result.success, result.error)
        self.assertIn("Running command:", output.getvalue())
        self.assertNotIn("source ", output.getvalue())
        self.assertFalse(self.url_file.exists())
        self.assertFalse(self.result_file.exists())

    def test_template_compiles_through_runner(self):
        workflow = self.home / "workflow.yaml"
        workflow.write_text(
            yaml.safe_dump(
                {
                    "template": "envelopes/ingest-job_desktop.yaml.j2",
                    "values": {
                        "definition": {"name": "Ingest", "location": "desktop"},
                        "url": self.url,
                    },
                }
            )
        )
        env = jinja_env.overlay(loader=FileSystemLoader(PROJECT_ROOT / "templates"))
        with (
            patch("yaml_compiler.jinja_env", env),
            patch("sys.argv", ["task", str(workflow)]),
            redirect_stdout(io.StringIO()),
        ):
            main()
        self.assertEqual(
            self.result_file.read_text(),
            f"transformed:{self.url}|true|original",
        )

    def test_factory_builds_ingestion_task(self):
        task = self.task()
        self.assertIsInstance(
            Task.from_dict("ingest-job", **task.model_dump()), IngestJob
        )

    def test_invalid_ingestion_output_is_a_failed_result(self):
        for output in (
            "not JSON",
            "{}",
            "[]",
            '{"id": null}',
            '{"id": 123}',
            '{"id": ""}',
            '{"id": " "}',
        ):
            with self.subTest(output=output):
                with patch("models.run") as run, redirect_stdout(io.StringIO()):
                    run.return_value.stdout = output
                    result = self.task()()
                self.assertFalse(result.success)
                self.assertIn("ingest-job did not return", result.error)
                self.assertIsNone(result.job_id)
                self.assertEqual(result.lookup_command, "")

    def test_lookup_command_quotes_the_database_id(self):
        job_id = "job id's"
        with patch("models.run") as run, redirect_stdout(io.StringIO()):
            run.return_value.stdout = '{"id": "job id\'s"}'
            result = self.task()()
        self.assertTrue(result.success, result.error)
        self.assertEqual(shlex.split(result.lookup_command), ["jds", "peek", "--id", job_id])

    def test_invalid_task_configuration_is_rejected(self):
        for overrides in (
            {"platform": "unsupported"},
            {"kind": "unsupported"},
            {"url": ""},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    IngestJob(**{**self.task().model_dump(), **overrides})


if __name__ == "__main__":
    unittest.main()
