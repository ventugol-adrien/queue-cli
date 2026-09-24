import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from models import (
    Email,
    Image2Video,
    RemoteInstanceRequest,
    ScalewayInstance,
    ScalewayInstanceRequest,
)
from runner import main, scw_start, scw_save, scw_stop


class ScalewayInstanceTests(unittest.TestCase):
    def setUp(self):
        temporary_home = TemporaryDirectory()
        self.addCleanup(temporary_home.cleanup)
        self.home = Path(temporary_home.name)
        home_patch = patch("models.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def stored_instances(self):
        database_path = self.home / ".local" / "share" / "scaleway" / "instances.db"
        with closing(sqlite3.connect(database_path)) as connection:
            return connection.execute(
                "SELECT zone, server_id, saved_image_id, deleted FROM instances"
            ).fetchall()

    @patch("models.run")
    def test_ids_persist_through_create_save_and_delete(self, run_command):
        run_command.side_effect = [
            Mock(stdout='{"id": "server-id"}'),
            Mock(stdout='{"state": "running"}'),
            Mock(stdout='{"state": "stopped"}'),
            Mock(stdout='{"id": "backup-id"}'),
            Mock(),
        ]
        instance = self.instance()
        instance.create()
        self.assertEqual(self.stored_instances(), [("fr-par-2", "server-id", None, 0)])
        instance.save()
        self.assertEqual(
            self.stored_instances(), [("fr-par-2", "server-id", "backup-id", 0)]
        )
        self.instance(server_id="server-id").delete()
        self.assertEqual(
            self.stored_instances(), [("fr-par-2", "server-id", "backup-id", 1)]
        )

    @patch("models.run")
    def test_id_is_persisted_before_wait_timeout(self, run_command):
        run_command.side_effect = [
            Mock(stdout='{"id": "server-id"}'),
            TimeoutExpired("scw", 300),
        ]
        with self.assertRaises(TimeoutExpired):
            self.instance().create()
        self.assertEqual(self.stored_instances(), [("fr-par-2", "server-id", None, 0)])

    @patch("models.run", side_effect=CalledProcessError(1, "scw"))
    def test_failed_delete_preserves_database_record(self, run_command):
        instance = self.instance(server_id="server-id", saved_image_id="backup-id")
        instance._record_state()
        with self.assertRaises(CalledProcessError):
            instance.delete()
        self.assertEqual(
            self.stored_instances(), [("fr-par-2", "server-id", "backup-id", 0)]
        )

    def instance(self, **overrides):
        return ScalewayInstance(
            vcpus=10, ram=42, storage=400, bandwidth=2000, **overrides
        )

    def test_load_restores_configuration_and_accounting(self):
        instance = self.instance(
            server_id="server-id",
            created_at=1000,
            hourly_price=2,
            currency="EUR",
            save_on_completion=True,
            timeout_minutes=12,
            image="image-id",
            tags=["test"],
        )
        with patch("models.time.time", return_value=1600):
            instance._record_state()
        restored = ScalewayInstance.load("server-id")
        self.assertEqual(restored.model_dump(), instance.model_dump())
        self.assertFalse(restored.dry_run)
        self.assertEqual(ScalewayInstance.load("server-id", "fr-par-2"), restored)
        with self.assertRaisesRegex(ValueError, "No recorded"):
            ScalewayInstance.load("server-id", "fr-par-1")

    def test_load_rejects_missing_and_terminated_instances(self):
        with self.assertRaisesRegex(ValueError, "No recorded"):
            ScalewayInstance.load("missing")
        instance = self.instance(server_id="server-id")
        instance._record_state(deleted=True)
        with self.assertRaisesRegex(ValueError, "already terminated"):
            ScalewayInstance.load("server-id")

    def test_load_requires_zone_for_ambiguous_id(self):
        self.instance(server_id="server-id", zone="fr-par-1")._record_state()
        self.instance(server_id="server-id", zone="fr-par-2")._record_state()
        with self.assertRaisesRegex(ValueError, "specify --zone"):
            ScalewayInstance.load("server-id")

    def test_load_defaults_to_oldest_active_record(self):
        self.instance(server_id="deleted", created_at=100)._record_state(deleted=True)
        self.instance(
            server_id="newer", created_at=300, zone="fr-par-1"
        )._record_state()
        self.instance(server_id="older", created_at=200, stopped_at=250)._record_state()
        self.assertEqual(ScalewayInstance.load().server_id, "older")
        self.assertEqual(ScalewayInstance.load(zone="fr-par-1").server_id, "newer")
        with self.assertRaisesRegex(ValueError, "No active"):
            ScalewayInstance.load(zone="nl-ams-1")

    def test_load_default_fails_when_all_records_are_deleted(self):
        self.instance(server_id="deleted")._record_state(deleted=True)
        with self.assertRaisesRegex(ValueError, "No active"):
            ScalewayInstance.load()

    @patch("models.run")
    def test_stores_uptime_and_estimated_cost_after_termination(self, run_command):
        instance = self.instance(
            server_id="server-id", created_at=1000, hourly_price=2, currency="EUR"
        )
        output = io.StringIO()
        with patch("models.time.time", return_value=2800), redirect_stdout(output):
            instance.delete()
        self.assertIn("Server server-id terminated.", output.getvalue())
        self.assertIn("Uptime: 1800.00s", output.getvalue())
        self.assertIn("Estimated compute cost: 1.000000 EUR", output.getvalue())
        database_path = self.home / ".local" / "share" / "scaleway" / "instances.db"
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT uptime_seconds, run_cost, hourly_price, currency, terminated_at, deleted FROM instances"
            ).fetchone()
        self.assertEqual(row, (1800, 1, 2, "EUR", 2800, 1))

    @patch("models.run")
    def test_termination_reports_unavailable_cost_without_accounting(self, run_command):
        output = io.StringIO()
        with redirect_stdout(output):
            self.instance(server_id="server-id").delete()
        self.assertIn("Estimated compute cost: unavailable", output.getvalue())

    @patch("models.run")
    def test_dry_run_does_not_report_completed_cost(self, run_command):
        output = io.StringIO()
        instance = self.instance(server_id="server-id", dry_run=True)
        with redirect_stdout(output):
            instance.delete()
        run_command.assert_not_called()
        self.assertNotIn("Estimated compute cost:", output.getvalue())
        self.assertNotIn("terminated.", output.getvalue())

    @patch("models.run")
    def test_backup_stop_ends_metering_even_if_backup_fails(self, run_command):
        instance = self.instance(
            server_id="server-id", created_at=1000, hourly_price=6, currency="EUR"
        )
        run_command.side_effect = [
            Mock(stdout='{"state": "running"}'),
            Mock(),
            CalledProcessError(1, "scw"),
        ]
        with patch("models.time.time", return_value=1600):
            with self.assertRaises(CalledProcessError):
                instance.save()
        run_command.side_effect = None
        resumed = self.instance(server_id="server-id")
        with patch("models.time.time", return_value=2800):
            resumed.delete()
        self.assertEqual(resumed.uptime_seconds, 600)
        self.assertEqual(resumed.run_cost, 1)
        self.assertEqual(resumed.stopped_at, 1600)
        self.assertEqual(resumed.terminated_at, 2800)
        self.assertEqual(resumed.currency, "EUR")

    def test_copies_legacy_database_without_inventing_accounting(self):
        legacy_path = self.home / ".local" / "state" / "scaleway" / "instances.db"
        legacy_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                "CREATE TABLE instances (zone TEXT, server_id TEXT, saved_image_id TEXT, "
                "deleted INTEGER, PRIMARY KEY (zone, server_id))"
            )
            connection.execute(
                "INSERT INTO instances VALUES ('fr-par-2', 'old-server', 'backup', 1)"
            )
            connection.commit()
        self.instance(server_id="new-server")._record_state()
        database_path = self.home / ".local" / "share" / "scaleway" / "instances.db"
        with closing(sqlite3.connect(database_path)) as connection:
            rows = connection.execute(
                "SELECT server_id, saved_image_id, uptime_seconds, run_cost FROM instances ORDER BY server_id"
            ).fetchall()
        self.assertEqual(
            rows,
            [("new-server", None, None, None), ("old-server", "backup", None, None)],
        )
        self.assertTrue(legacy_path.exists())

    @patch("models.run", side_effect=CalledProcessError(1, "scw"))
    def test_failed_termination_does_not_finalize_accounting(self, run_command):
        instance = self.instance(server_id="server-id", created_at=1000, hourly_price=2)
        with patch("models.time.time", return_value=1600):
            instance._record_state()
        output = io.StringIO()
        with patch("models.time.time", return_value=2800), redirect_stdout(output):
            with self.assertRaises(CalledProcessError):
                instance.delete()
        self.assertNotIn("Estimated compute cost:", output.getvalue())
        database_path = self.home / ".local" / "share" / "scaleway" / "instances.db"
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT deleted, terminated_at, uptime_seconds FROM instances"
            ).fetchone()
        self.assertEqual(row, (0, None, 600))

    @patch("models.run")
    def test_create_tracks_server_and_waits_for_running(self, run_command):
        run_command.side_effect = [
            Mock(stdout='{"id": "server-id"}'),
            Mock(stdout='{"state": "running"}'),
        ]
        instance = self.instance(tags=["worker", "job with spaces"])
        instance.create()
        self.assertEqual(instance.server_id, "server-id")
        self.assertEqual(
            run_command.call_args_list[0].args[0],
            [
                "scw",
                "instance",
                "server",
                "create",
                "type=RENDER-S",
                "image=base",
                "ip=new",
                "tags.0=worker",
                "tags.1=job with spaces",
                "zone=fr-par-2",
                "-o",
                "json",
            ],
        )
        run_command.assert_called_with(
            [
                "scw",
                "instance",
                "server",
                "wait",
                "server-id",
                "timeout=5m",
                "zone=fr-par-2",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

    @patch("models.run")
    def test_create_retains_id_when_wait_times_out(self, run_command):
        run_command.side_effect = [
            Mock(stdout='{"id": "server-id"}'),
            TimeoutExpired("scw", 300),
        ]
        instance = self.instance()
        with self.assertRaises(TimeoutExpired):
            instance.create()
        self.assertEqual(instance.server_id, "server-id")
        with self.assertRaisesRegex(ValueError, "already"):
            instance.create()
        self.assertEqual(run_command.call_count, 2)

    @patch("models.run")
    def test_create_surfaces_cli_error_without_retry(self, run_command):
        for stdout, stderr, expected in (
            ("", "image not found", "image not found"),
            ("quota exceeded", "", "quota exceeded"),
            (None, None, "No error output from scw"),
        ):
            with self.subTest(stderr=stderr, stdout=stdout):
                run_command.reset_mock()
                error = CalledProcessError(1, ["scw"], output=stdout, stderr=stderr)
                run_command.side_effect = error
                instance = self.instance()
                with self.assertRaisesRegex(RuntimeError, expected) as caught:
                    instance.create()
                self.assertIs(caught.exception.__cause__, error)
                self.assertIsNone(instance.server_id)
                run_command.assert_called_once()

    @patch("models.run")
    def test_create_rejects_non_running_result(self, run_command):
        run_command.side_effect = [
            Mock(stdout='{"id": "server-id"}'),
            Mock(stdout='{"state": "stopped"}'),
        ]
        instance = self.instance()
        with self.assertRaisesRegex(RuntimeError, "running"):
            instance.create()
        self.assertEqual(instance.server_id, "server-id")

    @patch("models.run")
    def test_save_stops_server_and_records_image(self, run_command):
        run_command.side_effect = [
            Mock(stdout='{"state": "running"}'),
            Mock(),
            Mock(stdout='{"id": "backup-id"}'),
        ]
        instance = self.instance(server_id="server-id")
        instance.save()
        self.assertEqual(instance.saved_image_id, "backup-id")
        self.assertEqual(instance.server_id, "server-id")
        self.assertEqual(
            [call.args[0][3] for call in run_command.call_args_list],
            ["get", "stop", "backup"],
        )
        self.assertIn("--wait", run_command.call_args_list[1].args[0])
        self.assertIn("--wait", run_command.call_args_list[2].args[0])

    @patch("models.run")
    def test_save_stopped_server_preserves_previous_backup_on_failure(
        self, run_command
    ):
        run_command.side_effect = [
            Mock(stdout='{"state": "stopped"}'),
            CalledProcessError(1, "scw"),
        ]
        instance = self.instance(server_id="server-id", saved_image_id="old-backup")
        with self.assertRaises(CalledProcessError):
            instance.save()
        self.assertEqual(instance.saved_image_id, "old-backup")
        self.assertEqual(instance.server_id, "server-id")
        self.assertEqual(run_command.call_args.args[0][3], "backup")

    @patch("models.run")
    def test_delete_removes_resources_but_keeps_backup(self, run_command):
        instance = self.instance(server_id="server-id", saved_image_id="backup-id")
        instance.delete()
        run_command.assert_called_once_with(
            [
                "scw",
                "instance",
                "server",
                "terminate",
                "server-id",
                "with-block=true",
                "with-ip=true",
                "--wait",
                "zone=fr-par-2",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertIsNone(instance.server_id)
        self.assertEqual(instance.saved_image_id, "backup-id")
        instance.delete()
        run_command.assert_called_once()

    @patch("models.run", side_effect=CalledProcessError(1, "scw"))
    def test_delete_retains_id_on_failure(self, run_command):
        instance = self.instance(server_id="server-id")
        with self.assertRaises(CalledProcessError):
            instance.delete()
        self.assertEqual(instance.server_id, "server-id")

    @patch("models.run")
    def test_invalid_lifecycle_calls_do_not_run_commands(self, run_command):
        with self.assertRaisesRegex(ValueError, "server_id"):
            self.instance().save()
        with self.assertRaisesRegex(ValueError, "timeout_minutes"):
            self.instance(timeout_minutes=0).create()
        with self.assertRaisesRegex(ValueError, "timeout_minutes"):
            self.instance(server_id="server-id", timeout_minutes=0).save()
        with self.assertRaisesRegex(ValueError, "timeout_minutes"):
            self.instance(server_id="server-id", timeout_minutes=0).delete()
        run_command.assert_not_called()


class ScalewayEntryPointTests(unittest.TestCase):
    @patch("runner.ScalewayInstance.load")
    def test_save_and_stop_default_to_first_active_instance(self, load):
        for entrypoint, command in ((scw_save, "scw-save"), (scw_stop, "scw-stop")):
            for save_enabled in (False, True):
                with self.subTest(command=command, save=save_enabled):
                    instance = Mock(
                        server_id="server-id",
                        zone="fr-par-2",
                        dry_run=False,
                        save_on_completion=save_enabled,
                        saved_image_id="backup-id",
                    )
                    instance.return_value = instance
                    load.reset_mock()
                    load.return_value = instance
                    with (
                        patch("sys.argv", [command]),
                        redirect_stdout(io.StringIO()) as output,
                    ):
                        entrypoint()
                    load.assert_called_once_with(None, None)
                    instance.assert_called_once_with(dry_run=False)
                    self.assertIn(
                        "Selected Scaleway server server-id", output.getvalue()
                    )
                    instance.create.assert_not_called()
                    if command == "scw-save" or save_enabled:
                        instance.save.assert_called_once()
                    else:
                        instance.save.assert_not_called()
                    if command == "scw-stop":
                        instance.delete.assert_called_once()
                    else:
                        instance.delete.assert_not_called()

    @patch("runner.ScalewayInstance.load")
    def test_explicit_id_zone_and_dry_run_are_forwarded(self, load):
        for entrypoint in (scw_save, scw_stop):
            instance = Mock(save_on_completion=False, dry_run=True)
            instance.return_value = instance
            load.reset_mock()
            load.return_value = instance
            with (
                patch(
                    "sys.argv", ["scw", "server-id", "--zone", "fr-par-1", "--dry-run"]
                ),
                redirect_stdout(io.StringIO()),
            ):
                entrypoint()
            load.assert_called_once_with("server-id", "fr-par-1")
            instance.assert_called_once_with(dry_run=True)

    @patch("runner.ScalewayInstance.load")
    def test_stop_terminates_even_if_save_fails(self, load):
        instance = Mock(save_on_completion=True)
        instance.return_value = instance
        instance.save.side_effect = RuntimeError("backup failed")
        load.return_value = instance
        with patch("sys.argv", ["scw-stop"]), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                scw_stop()
        instance.delete.assert_called_once()

    @patch(
        "runner.ScalewayInstance.load",
        side_effect=ValueError("No active Scaleway instances found"),
    )
    def test_no_active_instance_reports_cli_error(self, load):
        from contextlib import redirect_stderr

        with patch("sys.argv", ["scw-save"]), redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                scw_save()
        self.assertEqual(error.exception.code, 2)
        self.assertIn("No active", output.getvalue())

    @patch("models.ScalewayInstanceRequest.select")
    def test_start_uses_definition_and_leaves_server_running(self, select):
        definition = Path(__file__).parent / "fixtures" / "instances" / "render-s.yaml"
        for dry_run in (False, True):
            instance = Mock(server_id="server-id", zone="fr-par-2")
            instance.return_value = instance
            select.return_value = instance
            arguments = ["scw-start", str(definition)] + (
                ["--dry-run"] if dry_run else []
            )
            with patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
                scw_start()
            instance.assert_called_once_with(dry_run=dry_run)
            instance.create.assert_called_once()
            instance.save.assert_not_called()
            instance.delete.assert_not_called()


class RemoteInstanceRequestTests(unittest.TestCase):
    def test_range_endpoints_are_optional(self):
        for field in (
            "gpu_memory_rng",
            "vcpus_rng",
            "ram_rng",
            "storage_rng",
            "bandwidth_rng",
        ):
            for bounds in ((None, 160), (8, None), (None, None), (8, 160)):
                with self.subTest(field=field, bounds=bounds):
                    request = RemoteInstanceRequest(**{field: bounds})
                    self.assertEqual(getattr(request, field), bounds)

    def test_range_defaults_are_preserved(self):
        request = RemoteInstanceRequest()
        self.assertEqual(request.gpu_memory_rng, (8, 160))
        self.assertEqual(request.vcpus_rng, (4, 16))
        self.assertEqual(request.ram_rng, (16, 64))
        self.assertEqual(request.storage_rng, (100, 1000))
        self.assertEqual(request.bandwidth_rng, (100, 5000))


class InstanceSelectionTests(unittest.TestCase):
    @patch("models.run")
    def test_image_lookup_reports_cli_failure_without_retry(self, run_command):
        for stdout, stderr, expected in (
            ("", "access denied", "access denied"),
            ("service unavailable", "", "service unavailable"),
            (None, None, "No error output from scw"),
        ):
            with self.subTest(stdout=stdout, stderr=stderr):
                run_command.reset_mock()
                error = CalledProcessError(1, ["scw"], output=stdout, stderr=stderr)
                run_command.side_effect = error
                with self.assertRaisesRegex(RuntimeError, expected) as caught:
                    self.request(image="base")._resolve_image()
                self.assertIn("'base' in fr-par-2", str(caught.exception))
                self.assertIs(caught.exception.__cause__, error)
                run_command.assert_called_once()

    @patch("models.run")
    def test_resolves_custom_image_name_during_selection(self, run_command):
        image_id = "87dba51d-c51b-4fe8-ac72-57e904ac49bb"
        run_command.side_effect = [
            Mock(stdout=json.dumps([self.server_type()])),
            Mock(
                stdout=json.dumps(
                    [{"name": "base", "id": image_id, "state": "available"}]
                )
            ),
        ]
        request = self.request(image="base", zone="fr-par-2")
        self.assertEqual(request().image, image_id)
        self.assertEqual(request.image, "base")
        run_command.assert_called_with(
            ["scw", "instance", "image", "list", "zone=fr-par-2", "-o", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @patch("models.run")
    def test_image_resolution_rejects_ambiguous_or_unavailable_names(self, run_command):
        for images, message in (
            (
                [{"name": "base", "id": "first"}, {"name": "base", "id": "second"}],
                "Multiple images",
            ),
            ([{"name": "base", "id": "first", "state": "creating"}], "not available"),
        ):
            with self.subTest(message=message):
                run_command.return_value.stdout = json.dumps(images)
                with self.assertRaisesRegex(ValueError, message):
                    self.request(image="base")._resolve_image()

    @patch("models.run")
    def test_image_uuid_needs_no_lookup(self, run_command):
        image_id = "87dba51d-c51b-4fe8-ac72-57e904ac49bb"
        self.assertEqual(self.request(image=image_id)._resolve_image(), image_id)
        run_command.assert_not_called()

    @patch("models.run")
    def test_unmatched_image_label_is_left_for_cli(self, run_command):
        run_command.return_value.stdout = json.dumps(
            [
                {"name": "ubuntu_jammy-other", "id": "other", "state": "available"},
            ]
        )
        self.assertEqual(
            self.request(image="ubuntu_jammy")._resolve_image(), "ubuntu_jammy"
        )

    def request(self, **overrides):
        values = {
            field: (None, None)
            for field in (
                "gpu_memory_rng",
                "vcpus_rng",
                "ram_rng",
                "storage_rng",
                "bandwidth_rng",
            )
        }
        return ScalewayInstanceRequest(**(values | overrides))

    def server_type(self, **overrides):
        return {
            "name": "H100-SXM-8-80G",
            "availability": "available",
            "gpu": 8,
            "cpu": 128,
            "ram": 960 * 1024**3,
            "local_volume_max_size": 0,
            "bandwidth": 20000 * 10**6,
            "hourly_price": {"units": 25, "nanos": 330800000, "currency_code": "EUR"},
        } | overrides

    @patch("models.run")
    def test_selects_cheapest_available_match(self, run_command):
        run_command.return_value.stdout = json.dumps(
            [
                self.server_type(
                    name="A-expensive", gpu=0, hourly_price={"units": 26, "nanos": 0}
                ),
                self.server_type(),
                self.server_type(
                    name="sold-out",
                    availability="shortage",
                    hourly_price={"units": 0, "nanos": 1},
                ),
            ]
        )
        request = self.request(
            image="87dba51d-c51b-4fe8-ac72-57e904ac49bb", tags=["test"]
        )
        instance = request()
        self.assertEqual(instance.type, "H100-SXM-8-80G")
        self.assertIsInstance(instance, ScalewayInstance)
        self.assertEqual(instance.gpu_memory, 640)
        self.assertEqual(instance.ram, 960)
        self.assertEqual(instance.bandwidth, 20000)
        self.assertAlmostEqual(instance.hourly_price, 25.3308)
        self.assertEqual(instance.currency, "EUR")
        self.assertEqual(instance.image, "87dba51d-c51b-4fe8-ac72-57e904ac49bb")
        self.assertEqual(instance.tags, ["test"])
        run_command.assert_called_once_with(
            ["scw", "instance", "server-type", "list", "zone=fr-par-2", "-o", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @patch("models.run")
    def test_inclusive_and_open_bounds_for_every_resource(self, run_command):
        run_command.return_value.stdout = json.dumps([self.server_type()])
        for resource, value in (
            ("gpu_memory", 640),
            ("vcpus", 128),
            ("ram", 960),
            ("storage", 0),
            ("bandwidth", 20000),
        ):
            for bounds in ((value, value), (None, value), (value, None)):
                with self.subTest(resource=resource, bounds=bounds):
                    self.request(**{f"{resource}_rng": bounds})()
            for bounds in ((value + 1, None), (None, value - 1)):
                with self.subTest(resource=resource, bounds=bounds):
                    with self.assertRaisesRegex(ValueError, "No available"):
                        self.request(**{f"{resource}_rng": bounds})()

    @patch("models.run")
    def test_explicit_type_completes_concrete_instance(self, run_command):
        run_command.return_value.stdout = json.dumps(
            [
                self.server_type(
                    name="RENDER-S", gpu=1, hourly_price={"units": 1, "nanos": 0}
                ),
                self.server_type(),
            ]
        )
        request = ScalewayInstanceRequest(
            type="H100-SXM-8-80G",
            zone="fr-par-2",
            image="base",
            timeout_minutes=12,
            save=True,
            metadata={"job": "test"},
        )
        instance = request()
        self.assertIsInstance(instance, ScalewayInstance)
        self.assertEqual(instance.type, request.type)
        self.assertEqual(instance.vcpus, 128)
        self.assertEqual(instance.storage, 0)
        self.assertEqual(instance.timeout_minutes, 12)
        self.assertTrue(instance.save_on_completion)
        self.assertEqual(instance.metadata, {"job": "test"})

    @patch("models.run")
    def test_explicit_type_still_enforces_explicit_ranges(self, run_command):
        run_command.return_value.stdout = json.dumps([self.server_type()])
        with self.assertRaisesRegex(ValueError, "No available"):
            ScalewayInstanceRequest(type="H100-SXM-8-80G", vcpus_rng=(None, 16))()

    @patch("models.run")
    def test_reversed_range_is_rejected_before_lookup(self, run_command):
        with self.assertRaisesRegex(ValueError, "bandwidth_rng"):
            self.request(bandwidth_rng=(100, 50))()
        run_command.assert_not_called()

    @patch("models.run")
    def test_unknown_gpu_memory_cannot_match_a_bound(self, run_command):
        run_command.return_value.stdout = json.dumps([self.server_type(name="unknown")])
        with self.assertRaisesRegex(ValueError, "No available"):
            self.request(gpu_memory_rng=(1, None))()


class Image2VideoCommandTests(unittest.TestCase):
    def render_command(self, **kwargs):
        task = Image2Video(
            name="I2V",
            location="desktop",
            type="image2video",
            model="minimax_fp8",
            **kwargs,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            task(dry_run=True)
        return output.getvalue()

    def test_omits_empty_image_argument(self):
        command = self.render_command()

        self.assertNotIn("--image", command)

    def test_serializes_image_path(self):
        command = self.render_command(image=["/tmp/input.png"])

        self.assertIn("--image /tmp/input.png", command)
        self.assertNotIn("--image '[", command)


class InstanceWorkflowTests(unittest.TestCase):
    def test_deliveries_only_use_instance_context(self):
        for dry_run, delivery_fails in ((False, False), (False, True), (True, False)):
            with self.subTest(dry_run=dry_run, delivery_fails=delivery_fails):
                events = []
                instance = ScalewayInstance(
                    vcpus=10,
                    ram=42,
                    storage=400,
                    bandwidth=2000,
                    save_on_completion=False,
                )
                data = {
                    "definition": {"type": "delivery", "location": "scaleway"},
                    "instance": {"type": "RENDER-S"},
                    "deliveries": [
                        {
                            "type": "notification",
                            "endpoint": "https://example.test",
                            "title": "Test",
                        }
                    ],
                }

                def deliver():
                    events.append("deliver")
                    if delivery_fails:
                        raise RuntimeError("delivery failed")

                arguments = ["task", "workflow.yaml"] + (
                    ["--dry-run"] if dry_run else []
                )
                with (
                    patch("sys.argv", arguments),
                    patch("runner.parse", return_value=data),
                    patch(
                        "models.ScalewayInstanceRequest.select", return_value=instance
                    ),
                    patch.object(
                        ScalewayInstance,
                        "create",
                        side_effect=lambda: events.append("create"),
                    ),
                    patch.object(ScalewayInstance, "save") as save,
                    patch.object(
                        ScalewayInstance,
                        "delete",
                        side_effect=lambda: events.append("delete"),
                    ),
                    patch("models.Notification.deliver", side_effect=deliver),
                    patch("runner.Task.from_dict") as task_factory,
                    redirect_stdout(io.StringIO()),
                ):
                    if delivery_fails:
                        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                            main()
                    else:
                        main()
                self.assertEqual(
                    events,
                    (
                        ["create", "delete"]
                        if dry_run
                        else ["create", "deliver", "delete"]
                    ),
                )
                self.assertEqual(instance.dry_run, dry_run)
                save.assert_not_called()
                task_factory.assert_not_called()

    def test_deliveries_finish_before_context_cleanup(self):
        for failure in (None, "render", "deliver"):
            for save_enabled in (False, True):
                with self.subTest(failure=failure, save=save_enabled):
                    events = []
                    data = {
                        "definition": {"type": "text2image", "location": "remote"},
                        "task": {},
                        "instance": {"save": save_enabled},
                    }
                    instance = ScalewayInstance(
                        vcpus=10,
                        ram=42,
                        storage=400,
                        bandwidth=2000,
                        save_on_completion=save_enabled,
                    )
                    result = Mock()
                    result.model_dump.return_value = {}
                    task = Mock(
                        side_effect=lambda **kwargs: events.append("task") or result
                    )
                    task.model_dump.return_value = {}
                    delivery = Mock()
                    delivery.model_dump_json.return_value = "{}"

                    def render(context):
                        events.append("render")
                        if failure == "render":
                            raise RuntimeError("render failed")
                        return delivery

                    def deliver():
                        events.append("deliver")
                        if failure == "deliver":
                            raise RuntimeError("deliver failed")

                    delivery.render.side_effect = render
                    delivery.deliver.side_effect = deliver
                    with (
                        patch("sys.argv", ["task", "workflow.yaml"]),
                        patch("runner.parse", return_value=data),
                        patch("runner.BaseDelivery.from_list", return_value=[delivery]),
                        patch("runner.Task.from_dict", return_value=task),
                        patch(
                            "models.ScalewayInstanceRequest.select",
                            return_value=instance,
                        ),
                        patch.object(
                            ScalewayInstance,
                            "create",
                            side_effect=lambda: events.append("create"),
                        ),
                        patch.object(
                            ScalewayInstance,
                            "save",
                            side_effect=lambda: events.append("save"),
                        ),
                        patch.object(
                            ScalewayInstance,
                            "delete",
                            side_effect=lambda: events.append("delete"),
                        ),
                        redirect_stdout(io.StringIO()),
                    ):
                        if failure:
                            with self.assertRaisesRegex(
                                RuntimeError, f"{failure} failed"
                            ):
                                main()
                        else:
                            main()
                    expected = ["create", "task", "render"]
                    if failure != "render":
                        expected.append("deliver")
                    if save_enabled:
                        expected.append("save")
                    self.assertEqual(events, expected + ["delete"])

    def test_instance_lifecycle_respects_save_and_execution_mode(self):
        for location, dry_run, save in (
            ("remote", False, False),
            ("remote", False, True),
            ("remote", True, True),
            ("desktop", False, True),
        ):
            with self.subTest(location=location, dry_run=dry_run, save=save):
                data = {
                    "definition": {
                        "name": "test",
                        "location": location,
                        "type": "text2image",
                    },
                    "task": {"model": "test"},
                    "deliveries": [],
                }
                if location == "remote":
                    data["instance"] = {"type": "RENDER-S", "save": save}
                arguments = ["task", "workflow.yaml"]
                if dry_run:
                    arguments.append("--dry-run")
                task = Mock()
                task.model_dump.return_value = {}
                task.return_value.model_dump.return_value = {}
                instance = ScalewayInstance(
                    vcpus=10,
                    ram=42,
                    storage=400,
                    bandwidth=2000,
                    save_on_completion=save,
                )
                with (
                    patch("sys.argv", arguments),
                    patch("runner.parse", return_value=data),
                    patch("runner.Task.from_dict", return_value=task),
                    patch(
                        "models.ScalewayInstanceRequest.select",
                        autospec=True,
                        return_value=instance,
                    ) as select,
                    patch.object(ScalewayInstance, "create") as create,
                    patch.object(ScalewayInstance, "save") as save_instance,
                    patch.object(ScalewayInstance, "delete") as delete,
                    patch("models.run") as cloud_command,
                    redirect_stdout(io.StringIO()),
                ):
                    main()
                task.assert_called_once_with(dry_run=dry_run)
                cloud_command.assert_not_called()
                if location == "remote":
                    select.assert_called_once()
                    self.assertEqual(instance.dry_run, dry_run)
                    self.assertEqual(select.call_args.args[0].save_on_completion, save)
                    create.assert_called_once()
                    delete.assert_called_once()
                    if save:
                        save_instance.assert_called_once()
                    else:
                        save_instance.assert_not_called()
                else:
                    select.assert_not_called()
                    create.assert_not_called()
                    save_instance.assert_not_called()
                    delete.assert_not_called()

    def test_dry_run_previews_lifecycle_without_mutation(self):
        for save_enabled in (False, True):
            with self.subTest(save=save_enabled):
                instance = ScalewayInstance(
                    vcpus=10,
                    ram=42,
                    storage=400,
                    bandwidth=2000,
                    save_on_completion=save_enabled,
                )
                before = instance.model_dump()
                output = io.StringIO()
                with (
                    patch("models.run") as command,
                    patch.object(ScalewayInstance, "_record_state") as record,
                    patch("models.sqlite3.connect") as database,
                    redirect_stdout(output),
                ):
                    with instance(dry_run=True):
                        print("TASK AND DELIVERIES")
                command.assert_not_called()
                record.assert_not_called()
                database.assert_not_called()
                self.assertEqual(instance.model_dump(), before)
                text = output.getvalue()
                self.assertIn("scw instance server create", text)
                self.assertIn("scw instance server wait '<server-id>'", text)
                self.assertIn("scw instance server terminate '<server-id>'", text)
                self.assertIn("with-block=true", text)
                self.assertIn("with-ip=true", text)
                self.assertLess(
                    text.index("server create"), text.index("TASK AND DELIVERIES")
                )
                self.assertLess(
                    text.index("TASK AND DELIVERIES"), text.index("server terminate")
                )
                if save_enabled:
                    self.assertIn("server get '<server-id>'", text)
                    self.assertIn("server stop '<server-id>'", text)
                    self.assertIn("server backup '<server-id>'", text)
                    self.assertLess(
                        text.index("TASK AND DELIVERIES"), text.index("server backup")
                    )
                    self.assertLess(
                        text.index("server backup"), text.index("server terminate")
                    )
                else:
                    self.assertNotIn("server backup", text)
                    self.assertNotIn("server stop", text)

    def test_context_cleanup_order_and_failures(self):
        for save_enabled, task_fails, save_fails, delete_fails in (
            (False, False, False, False),
            (False, True, False, False),
            (True, False, False, False),
            (True, True, False, False),
            (True, False, True, False),
            (True, False, False, True),
            (True, True, True, True),
        ):
            with self.subTest(
                save_enabled=save_enabled,
                task=task_fails,
                save=save_fails,
                delete=delete_fails,
            ):
                instance = ScalewayInstance(
                    vcpus=10,
                    ram=42,
                    storage=400,
                    bandwidth=2000,
                    save_on_completion=save_enabled,
                )
                events = []

                def save():
                    events.append("save")
                    if save_fails:
                        raise RuntimeError("save failed")

                def delete():
                    events.append("delete")
                    if delete_fails:
                        raise RuntimeError("delete failed")

                with (
                    patch.object(
                        ScalewayInstance,
                        "create",
                        side_effect=lambda: events.append("create"),
                    ),
                    patch.object(ScalewayInstance, "save", side_effect=save),
                    patch.object(ScalewayInstance, "delete", side_effect=delete),
                ):
                    error = None
                    try:
                        with instance as active:
                            self.assertIs(active, instance)
                            events.append("task")
                            if task_fails:
                                raise RuntimeError("task failed")
                    except RuntimeError as caught:
                        error = caught
                    if task_fails or save_fails or delete_fails:
                        self.assertIsNotNone(error)
                    else:
                        self.assertIsNone(error)
                expected = ["create", "task"]
                if save_enabled:
                    expected.append("save")
                self.assertEqual(events, expected + ["delete"])


class DeliveryLoggingTests(unittest.TestCase):
    def test_attachment_logging_does_not_block_delivery(self):
        data = {
            "definition": {},
            "task": {"type": "image2video"},
            "deliveries": [
                {
                    "type": "email",
                    "to": "recipient@example.com",
                    "subject": "Complete",
                    "body": "Task finished",
                    "attachments": ["/tmp/output video.mp4"],
                }
            ],
        }
        task = Mock()
        task.model_dump.return_value = {}
        task.return_value.model_dump.return_value = {}
        output = io.StringIO()
        with (
            patch("sys.argv", ["task", "workflow.yaml"]),
            patch("runner.parse", return_value=data),
            patch("runner.Task.from_dict", return_value=task),
            patch.object(Email, "deliver", autospec=True) as deliver,
            redirect_stdout(output),
        ):
            main()

        logged_delivery = json.loads(output.getvalue().split("=" * 40 + "\n")[-2])
        self.assertEqual(logged_delivery["attachments"], ["/tmp/output video.mp4"])
        deliver.assert_called_once()
        self.assertEqual(
            deliver.call_args.args[0].attachments, [Path("/tmp/output video.mp4")]
        )


if __name__ == "__main__":
    unittest.main()
