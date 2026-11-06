import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from jinja2 import FileSystemLoader

from models import Email, IngestJob, IngestJobResult, Notification
from runner import main
from yaml_compiler import PROJECT_ROOT, jinja_env


class IngestJobEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.env = jinja_env.overlay(
            loader=FileSystemLoader(
                [
                    PROJECT_ROOT / "templates",
                    PROJECT_ROOT / "templates/tasks",
                    PROJECT_ROOT / "templates/envelopes",
                ]
            )
        )
        self.url = "https://jobs.ashbyhq.com/company/job?q='quoted'&x=1"

    def compile(self, **values):
        return yaml.safe_load(
            self.env.get_template("envelopes/ingest-job_desktop.yaml.j2").render(
                url=self.url, **values
            )
        )

    def notification(self, **overrides):
        data = self.compile(deliveries=[{"type": "notification", **overrides}])
        return Notification(**data["deliveries"][0])

    def context(self, success=True, job_id="job-123"):
        data = self.compile()
        task = IngestJob(**{**data["definition"], **data["task"]})
        result = task.build_result(
            success=success, error=None if success else "scrape failed"
        )
        result.duration = 2.5
        if success and job_id is not None:
            result.job_id = job_id
            result.lookup_command = f"jds peek --id {job_id}"
        return {
            "definition": data["definition"],
            "task": task.model_dump(exclude={"deliveries"}),
            "result": result.model_dump(),
        }

    def test_defaults_include_ashby_task_and_ingestion_definition(self):
        data = self.compile()
        self.assertEqual(data["definition"]["type"], "ingest-job")
        self.assertEqual(data["definition"]["location"], "desktop")
        self.assertEqual(
            data["task"],
            {
                "type": "ingest-job",
                "platform": "ashby",
                "kind": "job_description",
                "url": self.url,
            },
        )
        self.assertNotIn("instance", data)

    def test_runtime_expressions_survive_compile(self):
        notification = self.notification()
        for field in (notification.title, notification.body, *notification.tags[-2:]):
            self.assertIn("result.success", field)
        self.assertIn("result.lookup_command", notification.output)

    def test_success_and_failure_notifications_use_ingestion_fields(self):
        for success in (True, False):
            with self.subTest(success=success):
                notification = self.notification().render(self.context(success))
                status = "Succeeded." if success else "Failed."
                self.assertIn(status, notification.title)
                self.assertIn("(2.5s)", notification.title)
                self.assertIn(
                    "### Job Ingested" if success else "### Job Ingestion Failed",
                    notification.body,
                )
                self.assertIn("`ashby`", notification.body)
                self.assertIn("`job_description`", notification.body)
                self.assertIn(self.url, notification.body)
                self.assertEqual(
                    notification.output, "jds peek --id job-123" if success else ""
                )
                if success:
                    self.assertIn("`jds peek --id job-123`", notification.body)
                else:
                    self.assertNotIn("jds peek", notification.body)
                self.assertEqual(notification.attachment, "")
                self.assertEqual(
                    notification.tags,
                    [
                        "briefcase",
                        "desktop",
                        "white_check_mark" if success else "rotating_light",
                        "sparkles" if success else "x",
                    ],
                )
                self.assertNotIn("{{", notification.model_dump_json())
                self.assertNotIn("{%", notification.model_dump_json())
                if not success:
                    self.assertIn("scrape failed", notification.body)
                with patch("models.run") as run, redirect_stdout(io.StringIO()):
                    notification.deliver()
                command = run.call_args.args[0]
                self.assertNotIn("--data-binary", command)
                if success:
                    self.assertIn("Click: jds peek --id job-123", command)
                else:
                    self.assertFalse(any(arg.startswith("Click:") for arg in command))

    def test_success_without_database_id_has_no_lookup_command(self):
        notification = self.notification().render(self.context(job_id=None))
        self.assertEqual(notification.output, "")
        self.assertNotIn("jds peek", notification.body)

    def test_explicit_notification_fields_are_preserved(self):
        overrides = {
            "endpoint": "https://example.test/notifications",
            "title": "{{ definition.name }}",
            "body": "Custom body",
            "output": "",
            "attachment": "",
            "tags": [],
            "destination": "laptop",
            "priority": 5,
            "markdown": False,
        }
        notification = self.notification(**overrides)
        self.assertEqual(
            notification.model_dump(), {"type": "notification", **overrides}
        )
        rendered = notification.render(self.context())
        self.assertEqual(rendered.title, "Job Ingestion")
        self.assertEqual(rendered.output, "")
        self.assertEqual(rendered.tags, [])

    def test_empty_fields_disable_defaults(self):
        overrides = {"title": "", "body": "", "output": "", "attachment": "", "tags": []}
        notification = self.notification(**overrides).render(self.context())
        for field, value in overrides.items():
            self.assertEqual(getattr(notification, field), value)

    def test_definition_and_task_template_can_be_overridden(self):
        data = self.compile(
            definition={"name": "Custom", "description": "Importing", "location": "local"},
            task_definition="ingest_ashby_job.yaml.j2",
        )
        self.assertEqual(data["definition"]["name"], "Custom")
        self.assertEqual(data["definition"]["description"], "Importing")
        self.assertEqual(data["definition"]["location"], "local")
        self.assertEqual(data["task"]["url"], self.url)

    def test_omitted_or_empty_deliveries_do_not_send_notifications(self):
        for values in ({}, {"deliveries": []}):
            with self.subTest(values=values):
                self.assertEqual(self.compile(**values)["deliveries"], [])

    def test_other_delivery_types_keep_their_values(self):
        deliveries = [
            {
                "type": "email",
                "to": "test@example.test",
                "subject": "Ingested",
                "body": "",
            },
            {"type": "download", "path": "/tmp/downloads"},
        ]
        data = self.compile(deliveries=deliveries)
        self.assertEqual(
            data["deliveries"],
            [
                {**deliveries[0], "body": "", "attachments": []},
                deliveries[1],
            ],
        )

    def test_email_defaults_render_success_and_failure(self):
        data = self.compile(deliveries=[{"type": "email", "to": "jobs@example.test"}])
        email = Email(**data["deliveries"][0])
        self.assertIn("result.success", email.subject)
        self.assertIn("result.success", email.body)
        for success in (True, False):
            with self.subTest(success=success):
                rendered = email.render(self.context(success))
                notification = self.notification().render(self.context(success))
                self.assertEqual(rendered.to, "jobs@example.test")
                self.assertEqual(rendered.subject, notification.title)
                self.assertEqual(rendered.body, notification.body)
                self.assertEqual(rendered.attachments, [])
                self.assertIn(self.url, rendered.body)
                self.assertNotIn("{{", rendered.model_dump_json())
                if not success:
                    self.assertIn("scrape failed", rendered.body)

    def test_explicit_email_fields_are_preserved(self):
        delivery = {
            "type": "email",
            "to": "jobs@example.test",
            "subject": "Custom: {{ definition.name }}",
            "body": "{{ task.url }}",
            "attachments": ["/tmp/report.txt"],
        }
        data = self.compile(deliveries=[delivery])
        self.assertEqual(data["deliveries"], [delivery])
        email = Email(**data["deliveries"][0]).render(self.context())
        self.assertEqual(email.subject, "Custom: Job Ingestion")
        self.assertEqual(email.body, self.url)
        self.assertEqual(email.attachments, [Path("/tmp/report.txt")])

    def test_empty_email_fields_disable_defaults(self):
        delivery = {
            "type": "email",
            "to": "jobs@example.test",
            "subject": "",
            "body": "",
            "attachments": [],
        }
        self.assertEqual(self.compile(deliveries=[delivery])["deliveries"], [delivery])

    def test_runner_renders_notifications_after_task_and_skips_dry_run_delivery(self):
        with TemporaryDirectory() as temporary:
            workflow = Path(temporary) / "ingest.yaml"
            workflow.write_text(
                yaml.safe_dump(
                    {
                        "template": "ingest-job_desktop.yaml.j2",
                        "values": {
                            "url": self.url,
                            "deliveries": [
                                {"type": "notification"},
                                {"type": "email", "to": "jobs@example.test"},
                            ],
                        },
                    }
                )
            )
            for success, dry_run in ((True, False), (False, False), (True, True)):
                with self.subTest(success=success, dry_run=dry_run):
                    with (
                        patch("yaml_compiler.jinja_env", self.env),
                        patch(
                            "sys.argv",
                            ["task", str(workflow)] + (["--dry-run"] if dry_run else []),
                        ),
                        patch.object(IngestJob, "execute", autospec=True) as execute,
                        patch.object(Notification, "deliver", autospec=True) as deliver,
                        patch.object(Email, "deliver", autospec=True) as email_deliver,
                        redirect_stdout(io.StringIO()),
                    ):
                        if success:
                            execute.return_value = IngestJobResult(
                                **self.context(job_id=None if dry_run else "job-123")[
                                    "result"
                                ]
                            )
                        else:
                            execute.side_effect = RuntimeError("scrape failed")
                        main()
                    execute.assert_called_once()
                    self.assertEqual(execute.call_args.kwargs["dry_run"], dry_run)
                    if dry_run:
                        deliver.assert_not_called()
                        email_deliver.assert_not_called()
                    else:
                        deliver.assert_called_once()
                        email_deliver.assert_called_once()
                        rendered = deliver.call_args.args[0]
                        email = email_deliver.call_args.args[0]
                        self.assertEqual(email.subject, rendered.title)
                        self.assertEqual(email.body, rendered.body)
                        self.assertEqual(email.to, "jobs@example.test")
                        self.assertIn(
                            "Succeeded." if success else "Failed.", rendered.title
                        )
                        self.assertEqual(
                            rendered.output, "jds peek --id job-123" if success else ""
                        )
                        if not success:
                            self.assertIn("scrape failed", rendered.body)


if __name__ == "__main__":
    unittest.main()
