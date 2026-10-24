import io
import json
import os
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import yaml

from models import (
    Email,
    Image2Video,
    RemoteInstanceRequest,
    ScalewayInstance,
    ScalewayInstanceRequest,
)
from runner import main, scw_start, scw_save, scw_stop, scw_volume
import volumes


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

    def test_schema_migration_backfills_server_type(self):
        database_path = self.home / ".local" / "share" / "scaleway" / "instances.db"
        database_path.parent.mkdir(parents=True)
        existing = self.instance(server_id="old-server", type="H100-SXM-8-80G")
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("""CREATE TABLE instances (
                    zone TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    saved_image_id TEXT,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    created_at REAL,
                    stopped_at REAL,
                    terminated_at REAL,
                    uptime_seconds REAL,
                    hourly_price REAL,
                    currency TEXT,
                    run_cost REAL,
                    config_json TEXT,
                    PRIMARY KEY (zone, server_id)
                )""")
            connection.execute(
                "INSERT INTO instances (zone, server_id, config_json) VALUES (?, ?, ?)",
                (existing.zone, existing.server_id, existing.model_dump_json()),
            )
            connection.commit()

        self.instance(server_id="new-server")._record_state()

        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT server_type FROM instances WHERE server_id = 'old-server'"
            ).fetchone()
        self.assertEqual(row, ("H100-SXM-8-80G",))

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
            server_id="server-id",
            type="H100-SXM-8-80G",
            created_at=1000,
            hourly_price=2,
            currency="EUR",
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
                "SELECT server_type, uptime_seconds, run_cost, hourly_price, currency, terminated_at, deleted FROM instances"
            ).fetchone()
        self.assertEqual(row, ("H100-SXM-8-80G", 1800, 1, 2, "EUR", 2800, 1))

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

    @patch("models.run")
    def test_delete_uses_delete_for_recorded_stopped_server(self, run_command):
        instance = self.instance(
            server_id="server-id", stopped_at=1000, saved_image_id="backup-id"
        )
        instance.delete()
        run_command.assert_called_once_with(
            [
                "scw",
                "instance",
                "server",
                "delete",
                "server-id",
                "with-volumes=all",
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

    @patch("models.run")
    def test_terminate_falls_back_only_for_stopped_state(self, run_command):
        error = CalledProcessError(
            1,
            "scw",
            stderr=json.dumps(
                {
                    "error": "scaleway-sdk-go: precondition failed: , invalidstate 'stopped' for the action 'terminate'"
                }
            ),
        )
        for fails in (False, True):
            with self.subTest(fallback_fails=fails):
                run_command.reset_mock()
                run_command.side_effect = [
                    error,
                    (
                        CalledProcessError(1, "scw", stderr="delete rejected")
                        if fails
                        else Mock()
                    ),
                ]
                instance = self.instance(
                    server_id="server-id", saved_image_id="backup-id"
                )
                with patch.object(ScalewayInstance, "_record_state") as record:
                    if fails:
                        with self.assertRaises(CalledProcessError) as caught:
                            instance.delete()
                        self.assertIn("delete rejected", caught.exception.__notes__[0])
                        self.assertEqual(instance.server_id, "server-id")
                        record.assert_not_called()
                    else:
                        instance.delete()
                        self.assertIsNone(instance.server_id)
                        record.assert_called_once_with(deleted=True)
                self.assertEqual(
                    [call.args[0][3] for call in run_command.call_args_list],
                    ["terminate", "delete"],
                )
                self.assertEqual(instance.saved_image_id, "backup-id")

    @patch("models.run")
    def test_stopped_server_dry_run_previews_delete(self, run_command):
        output = io.StringIO()
        with redirect_stdout(output):
            self.instance(server_id="server-id", stopped_at=1000, dry_run=True).delete()
        self.assertIn("server delete server-id with-volumes=all", output.getvalue())
        self.assertNotIn("server terminate", output.getvalue())
        run_command.assert_not_called()

    @patch(
        "models.run",
        side_effect=CalledProcessError(1, "scw", stderr="termination rejected"),
    )
    def test_delete_retains_id_on_failure(self, run_command):
        instance = self.instance(server_id="server-id")
        with self.assertRaises(CalledProcessError) as caught:
            instance.delete()
        self.assertEqual(instance.server_id, "server-id")
        self.assertIn("termination rejected", caught.exception.__notes__[0])
        run_command.assert_called_once()

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
    @patch("runner.ScalewayInstance.load_operation")
    @patch("runner.volumes.get_operation", return_value={"state": "prefetching"})
    def test_stop_recovers_operation_without_target_server(self, get_operation, load):
        instance = Mock(
            server_id=None,
            operation_id="operation-id",
            zone="fr-par-2",
            dry_run=False,
            save_on_completion=True,
        )
        instance.return_value = instance
        load.return_value = instance
        with (
            patch("sys.argv", ["scw-stop", "--operation-id", "operation-id"]),
            redirect_stdout(io.StringIO()) as output,
        ):
            scw_stop()
        load.assert_called_once_with("operation-id")
        get_operation.assert_called_once_with("operation-id")
        instance.save.assert_not_called()
        instance.delete.assert_called_once()
        self.assertIn("Skipping save", output.getvalue())

    @patch("runner.ScalewayInstance.load")
    def test_save_and_stop_default_to_first_active_instance(self, load):
        for entrypoint, command in ((scw_save, "scw-save"), (scw_stop, "scw-stop")):
            for save_enabled in (False, True):
                with self.subTest(command=command, save=save_enabled):
                    instance = Mock(
                        type="DEV1-M",
                        server_id="server-id",
                        operation_id=None,
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
                    self.assertIn("Selected instance type: DEV1-M", output.getvalue())
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
            instance = Mock(type="DEV1-M", server_id="server-id", zone="fr-par-2")
            instance.return_value = instance
            select.return_value = instance
            arguments = ["scw-start", str(definition)] + (
                ["--dry-run"] if dry_run else []
            )
            with patch("sys.argv", arguments), redirect_stdout(io.StringIO()) as output:
                scw_start()
            self.assertIn("Selected instance type: DEV1-M", output.getvalue())
            instance.assert_called_once_with(dry_run=dry_run)
            instance.create.assert_called_once()
            instance.save.assert_not_called()
            instance.delete.assert_not_called()

    @patch("models.ScalewayInstanceRequest.select", autospec=True)
    def test_start_loads_volume_definition_path_relative_to_instance(self, select):
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            volume_path = root / "volume.yaml"
            volume_path.write_text(
                "name: models-prefetched\n" "mount_path: /mnt/models\n"
            )
            definition = root / "instance.yaml"
            definition.write_text(
                "provider: scaleway\n" "type: RENDER-S\n" 'volume: "volume.yaml"\n'
            )
            instance = Mock(server_id="server-id", zone="fr-par-2")
            instance.return_value = instance
            select.return_value = instance
            with patch("sys.argv", ["scw-start", str(definition)]):
                with redirect_stdout(io.StringIO()):
                    scw_start()
        request = select.call_args.args[0]
        self.assertEqual(request.volume.name, "models-prefetched")
        self.assertEqual(request.volume.mount_path, "/mnt/models")
        instance.create.assert_called_once_with()

    def test_scw_volume_dry_run_previews_build_without_provisioning(self):
        with TemporaryDirectory() as temporary_dir:
            worker_definition = Path(temporary_dir) / "worker.yaml"
            worker_definition.write_text("provider: scaleway\ntype: DEV1-M\n")
            worker = Mock(type="DEV1-M", server_id=None)
            worker_request = Mock(
                volume=None,
                zone="fr-par-2",
                return_value=worker,
            )
            arguments = [
                "scw-volume",
                "create",
                "sd_t2i_illustrious.yaml",
                "--worker-definition",
                str(worker_definition),
                "--dry-run",
            ]
            with (
                patch(
                    "runner.RemoteInstanceRequest.from_dict",
                    return_value=worker_request,
                ),
                patch("sys.argv", arguments),
                redirect_stdout(io.StringIO()) as output,
            ):
                scw_volume()

        rendered = output.getvalue()
        self.assertIn("[dry-run] create volume worker DEV1-M", rendered)
        self.assertIn("build-volume --worker-id '<worker-id>'", rendered)
        self.assertIn("--prefix models/sdxl", rendered)
        self.assertIn("resolve volume sd-t2i-illustrious", rendered)
        worker.create.assert_not_called()
        worker.delete.assert_not_called()

    def test_scw_volume_loads_user_dirs_recipe_without_mountpoint(self):
        with TemporaryDirectory() as temporary_dir:
            home = Path(temporary_dir)
            definitions = home / ".config" / "scw" / "volume_definitions"
            definitions.mkdir(parents=True)
            (definitions / "user-models.yaml").write_text(
                "name: user-models\n"
                "bucket: model-bucket\n"
                "dirs:\n"
                "  - models/checkpoints/\n"
                "size_gb: 50\n"
                "zone: fr-par-2\n"
            )
            worker_definition = home / "worker.yaml"
            worker_definition.write_text("provider: scaleway\ntype: DEV1-M\n")
            worker = Mock(type="DEV1-M", server_id=None)
            worker_request = Mock(
                volume=None,
                zone="fr-par-2",
                return_value=worker,
            )
            with (
                patch("runner.Path.home", return_value=home),
                patch(
                    "runner.RemoteInstanceRequest.from_dict",
                    return_value=worker_request,
                ),
                patch(
                    "sys.argv",
                    [
                        "scw-volume",
                        "create",
                        "user-models.yaml",
                        "--worker-definition",
                        str(worker_definition),
                        "--dry-run",
                    ],
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                scw_volume()

        rendered = output.getvalue()
        self.assertIn("--bucket model-bucket", rendered)
        self.assertIn("--prefix models/checkpoints", rendered)
        self.assertIn("resolve volume user-models", rendered)
        self.assertIn(
            "Volume reference for an instance (set mount_path there)", rendered
        )
        self.assertNotIn("mount_path:", rendered)
        worker.create.assert_not_called()
        worker.delete.assert_not_called()

    @patch(
        "runner.volumes.find_block_volume_by_name", return_value="retained-volume-id"
    )
    @patch("runner.subprocess.run")
    def test_scw_volume_uses_builder_path_and_reports_remote_failure(
        self, ssh_run, find_volume
    ):
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            recipe_path = root / "recipe.yaml"
            recipe_path.write_text(
                "name: test-volume\n"
                "bucket: model-bucket\n"
                "size_gb: 50\n"
                "prefixes:\n"
                "  - models/checkpoints\n"
                "zone: fr-par-2\n"
            )
            worker_path = root / "worker.yaml"
            worker_path.write_text("provider: scaleway\ntype: DEV1-M\n")
            worker = Mock(type="DEV1-M", server_id="worker-id")
            worker.return_value = worker
            worker_request = Mock(volume=None, zone="fr-par-2", return_value=worker)
            ssh_run.side_effect = CalledProcessError(
                2,
                ["ssh"],
                output="Syncing source\n",
                stderr="Volume retained: retained-volume-id\n",
            )
            arguments = [
                "scw-volume",
                "create",
                str(recipe_path),
                "--worker-definition",
                str(worker_path),
            ]
            with (
                patch(
                    "runner.RemoteInstanceRequest.from_dict",
                    return_value=worker_request,
                ),
                patch("sys.argv", arguments),
                redirect_stdout(io.StringIO()) as stdout,
                redirect_stderr(io.StringIO()) as stderr,
            ):
                with self.assertRaisesRegex(RuntimeError, "exited with status 2"):
                    scw_volume()

        remote_command = ssh_run.call_args.args[0][2]
        self.assertEqual(ssh_run.call_args.args[0][:2], ["ssh", "-tt"])
        self.assertNotIn("capture_output", ssh_run.call_args.kwargs)
        self.assertIn('exec "$HOME/.local/bin/build-volume"', remote_command)
        self.assertIn("Syncing source", stdout.getvalue())
        self.assertIn("Volume retained: retained-volume-id", stderr.getvalue())
        self.assertIn(
            "Volume retained after build failure: retained-volume-id", stderr.getvalue()
        )
        find_volume.assert_called_once_with("test-volume", zone="fr-par-2")
        worker.delete.assert_called_once()

    @patch("runner.volumes.delete_block_volume")
    @patch("runner.volumes.resolve_block_volume", return_value="volume-uuid")
    def test_scw_volume_delete_accepts_explicit_name_or_id(
        self, resolve_volume, delete_volume
    ):
        for option, reference in (
            ("--volume-name", "models-prefetched"),
            ("--volume-id", "12345678-1234-4234-8234-123456789abc"),
        ):
            with self.subTest(option=option):
                resolve_volume.reset_mock()
                delete_volume.reset_mock()
                with (
                    patch(
                        "sys.argv",
                        ["scw-volume", "delete", option, reference, "--yes"],
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    scw_volume()

                resolve_volume.assert_called_once_with(
                    volume_id=reference if option == "--volume-id" else None,
                    name=reference if option == "--volume-name" else None,
                    zone="fr-par-2",
                )
                delete_volume.assert_called_once_with("volume-uuid", zone="fr-par-2")


class RemoteInstanceRequestTests(unittest.TestCase):
    def test_instance_volume_requires_name_or_id_and_safe_mount_path(self):
        for volume, message in (
            ({"mount_path": "/mnt/models"}, "exactly one of name or id"),
            (
                {
                    "name": "models",
                    "id": "12345678-1234-4234-8234-123456789abc",
                    "mount_path": "/mnt/models",
                },
                "exactly one of name or id",
            ),
            ({"id": "bad", "mount_path": "/mnt/models"}, "volume id is invalid"),
            ({"name": "models", "mount_path": "/mnt/../models"}, "parent segments"),
        ):
            with self.subTest(volume=volume):
                with self.assertRaisesRegex(ValueError, message):
                    RemoteInstanceRequest(volume=volume)

        for mount_path in ("/home/adrien/.local/share/models", "/etc/models"):
            with self.subTest(mount_path=mount_path):
                request = RemoteInstanceRequest(
                    volume={"name": "models", "mount_path": mount_path}
                )
                self.assertEqual(request.volume.mount_path, mount_path)

    def test_volume_rejects_conflicting_user_cloud_init_mount(self):
        volume = {
            "id": "12345678-1234-4234-8234-123456789abc",
            "mount_path": "/mnt/models",
        }
        with self.assertRaisesRegex(ValueError, "already configures mount path"):
            RemoteInstanceRequest(
                volume=volume,
                **{"cloud-init": {"mounts": [["/dev/sdb", "/mnt/models", "ext4"]]}},
            )

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
        self.assertEqual(request.gpu_memory_rng, (None, None))
        self.assertEqual(request.vcpus_rng, (None, None))
        self.assertEqual(request.ram_rng, (None, None))
        self.assertEqual(request.storage_rng, (None, None))
        self.assertEqual(request.bandwidth_rng, (None, None))


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
    def test_selects_gpu_without_local_volume(self, run_command):
        run_command.return_value.stdout = json.dumps(
            [
                self.server_type(
                    name="L4-1-24G",
                    gpu=1,
                    cpu=8,
                    ram=48 * 1024**3,
                    local_volume_max_size=0,
                    bandwidth=2500 * 10**6,
                )
            ]
        )

        instance = ScalewayInstanceRequest(
            image="87dba51d-c51b-4fe8-ac72-57e904ac49bb"
        )()

        self.assertEqual(instance.type, "L4-1-24G")
        self.assertEqual(instance.gpu_memory, 24)
        self.assertEqual(instance.storage, 0)

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


class VolumeOperationTests(unittest.TestCase):
    def setUp(self):
        temporary_home = TemporaryDirectory()
        self.addCleanup(temporary_home.cleanup)
        self.home = Path(temporary_home.name)
        home_patch = patch("volumes.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def test_create_volume_uses_decimal_gigabytes_and_operation_tag(self):
        operation_id = "12345678-1234-4234-8234-123456789abc"
        run_command = Mock(return_value=Mock(stdout='{"id":"volume-id"}'))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                volumes.create_block_volume(
                    zone="fr-par-2",
                    size_gb=50,
                    operation_id=operation_id,
                    run_command=run_command,
                ),
                "volume-id",
            )
        self.assertIn("Requesting a 50GB block volume", output.getvalue())
        self.assertIn("Block volume created: volume-id", output.getvalue())
        arguments = run_command.call_args.args[0]
        self.assertIn("from-empty.size=50GB", arguments)
        self.assertIn(f"tags.0=queue-prefetch-{operation_id}", arguments)

    def test_find_block_volume_by_name_queries_exact_zone_and_name(self):
        run_command = Mock(
            return_value=Mock(
                stdout=json.dumps(
                    [
                        {"id": "volume-id", "name": "queue-prefetch-operation"},
                        {"id": "other-id", "name": "other-volume"},
                    ]
                )
            )
        )
        self.assertEqual(
            volumes.find_block_volume_by_name(
                "queue-prefetch-operation",
                zone="fr-par-2",
                run_command=run_command,
            ),
            "volume-id",
        )
        self.assertEqual(
            run_command.call_args.args[0],
            [
                "scw",
                "block",
                "volume",
                "list",
                "name=queue-prefetch-operation",
                "zone=fr-par-2",
                "-o",
                "json",
            ],
        )

    def test_instance_volume_requires_one_existing_reference_and_mountpoint(self):
        with self.assertRaisesRegex(ValueError, "exactly one of name or id"):
            ScalewayInstance(
                type="RENDER-S",
                vcpus=8,
                ram=32,
                storage=0,
                bandwidth=1000,
                volume={"mount_path": "/mnt/models"},
            )
        with self.assertRaisesRegex(ValueError, "exactly one of name or id"):
            ScalewayInstance(
                type="RENDER-S",
                vcpus=8,
                ram=32,
                storage=0,
                bandwidth=1000,
                volume={
                    "id": "12345678-1234-4234-8234-123456789abc",
                    "name": "models",
                    "mount_path": "/mnt/models",
                },
            )

    def test_volume_command_failure_includes_provider_response(self):
        error = CalledProcessError(
            1,
            ["scw", "block", "volume", "create"],
            stderr="permission denied for zone fr-par-2",
        )
        with self.assertRaises(CalledProcessError) as caught:
            volumes._cli(
                ["scw", "block", "volume", "create"],
                run_command=Mock(side_effect=error),
            )
        self.assertIn(
            "permission denied for zone fr-par-2", caught.exception.__notes__[0]
        )

    @patch("volumes.get_server")
    def test_delete_helper_detaches_data_volume_before_terminating(self, get_server):
        get_server.return_value = {
            "state": "running",
            "volumes": {
                "0": {"id": "root-volume"},
                "1": {"id": "prefetch-volume"},
            },
        }
        run_command = Mock()
        volumes.delete_helper(
            "helper-id",
            zone="fr-par-2",
            volume_id="prefetch-volume",
            run_command=run_command,
        )
        commands = [call.args[0] for call in run_command.call_args_list]
        self.assertEqual(
            [command[3] for command in commands], ["stop", "update", "terminate"]
        )
        self.assertIn("volume-ids.0=root-volume", commands[1])
        self.assertNotIn("prefetch-volume", commands[1])
        self.assertIn("with-block=false", commands[2])

    def test_operation_journal_persists_ids_without_credentials(self):
        operation_id = volumes.create_operation(
            "fr-par-2",
            {"bucket": "models", "dirs": ["weights/"]},
        )
        volumes.update_operation(
            operation_id,
            volume_id="12345678-1234-4234-8234-123456789abc",
            state="prefetching",
        )
        operation = volumes.get_operation(operation_id)
        self.assertEqual(operation["state"], "prefetching")
        self.assertIsNone(operation["helper_server_id"])
        self.assertEqual(operation["config"]["bucket"], "models")
        self.assertNotIn("secret", json.dumps(operation).lower())

    def test_operation_host_keys_use_private_per_operation_directory(self):
        path = volumes.operation_known_hosts_path("operation-123")
        self.assertEqual(path.name, "operation-123")
        self.assertEqual(path.parent.name, "known_hosts")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_server_host_keys_use_private_per_server_directory(self):
        path = volumes.server_known_hosts_path("server-123")
        self.assertEqual(path.name, "server-server-123")
        self.assertEqual(path.parent.name, "known_hosts")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    @patch("volumes.get_server")
    def test_target_mount_verification_uses_server_specific_known_hosts(
        self, get_server
    ):
        get_server.return_value = {
            "public_ips": [{"address": "192.0.2.10"}],
        }
        run_command = Mock()
        with patch(
            "volumes.server_known_hosts_path", return_value=Path("/tmp/server-hosts")
        ):
            volumes.verify_target_mount(
                server_id="server-123",
                zone="fr-par-2",
                config={"mount_path": "/mnt/models", "ssh_user": "adrien"},
                timeout_minutes=1,
                run_command=run_command,
            )
        command = run_command.call_args.args[0]
        self.assertIn("adrien@192.0.2.10", command)
        self.assertIn("UserKnownHostsFile=/tmp/server-hosts", command)
        self.assertIn("GlobalKnownHostsFile=/dev/null", command)

    def test_helper_ssh_fails_fast_with_diagnostic_and_isolated_host_keys(self):
        failure = CalledProcessError(
            255,
            ["ssh"],
            stderr="Permission denied (publickey).",
        )
        run_command = Mock(side_effect=failure)
        with self.assertRaisesRegex(RuntimeError, r"Permission denied \(publickey\)"):
            volumes.wait_for_helper_cloud_init(
                host="192.0.2.10",
                config={
                    "prefetch_timeout_minutes": 10,
                    "ssh_known_hosts_file": "/tmp/op-known-hosts",
                },
                run_command=run_command,
            )
        run_command.assert_called_once()
        command = run_command.call_args.args[0]
        self.assertIn("ubuntu@192.0.2.10", command)
        self.assertIn("UserKnownHostsFile=/tmp/op-known-hosts", command)
        self.assertIn("GlobalKnownHostsFile=/dev/null", command)

    def test_operation_load_restores_partial_resource_ids(self):
        instance = ScalewayInstance(
            type="RENDER-S",
            vcpus=8,
            ram=32,
            storage=0,
            bandwidth=1000,
            volume={
                "name": "models-prefetched",
                "mount_path": "/mnt/models",
            },
        )
        operation_id = volumes.create_operation(
            "fr-par-2", instance.model_dump(mode="json")
        )
        volume_id = "12345678-1234-4234-8234-123456789abc"
        volume_uuid = "12345678-1234-4234-8234-123456789abc"
        volumes.update_operation(
            operation_id,
            volume_id=volume_id,
            volume_uuid=volume_uuid,
            state="volume_ready",
        )
        restored = ScalewayInstance.load_operation(operation_id)
        self.assertEqual(restored.operation_id, operation_id)
        self.assertEqual(restored.volume_id, volume_id)
        self.assertEqual(restored.volume_uuid, volume_uuid)
        self.assertIsNone(restored.server_id)

    @patch("models.run")
    def test_stopping_partial_operation_deletes_detached_volume(self, run_command):
        operation_id = volumes.create_operation(
            "fr-par-2", {"type": "RENDER-S", "volume": {"size_gb": 50}}
        )
        volume_id = "12345678-1234-4234-8234-123456789abc"
        volumes.update_operation(
            operation_id, volume_id=volume_id, state="volume_ready"
        )
        instance = ScalewayInstance(
            type="RENDER-S",
            vcpus=8,
            ram=32,
            storage=0,
            bandwidth=1000,
            operation_id=operation_id,
            volume_id=volume_id,
        )
        instance.delete()
        self.assertEqual(
            run_command.call_args.args[0],
            [
                "scw",
                "block",
                "volume",
                "delete",
                volume_id,
                "zone=fr-par-2",
                "-o",
                "json",
            ],
        )
        self.assertEqual(volumes.get_operation(operation_id)["state"], "deleted")

    @patch("models.run")
    def test_stopped_server_not_found_does_not_block_volume_deletion(self, run_command):
        run_command.side_effect = [
            CalledProcessError(1, "scw", stderr="server not found"),
            Mock(),
            Mock(),
        ]
        instance = ScalewayInstance(
            server_id="server-id",
            stopped_at=1,
            volume_id="12345678-1234-4234-8234-123456789abc",
            vcpus=8,
            ram=32,
            storage=0,
            bandwidth=1000,
        )
        with patch.object(instance, "_record_state"):
            instance.delete()
        self.assertEqual(
            run_command.call_args_list[2].args[0][1:4],
            ["block", "volume", "delete"],
        )

    @patch(
        "models.run", side_effect=CalledProcessError(1, "scw", stderr="access denied")
    )
    def test_failed_volume_cleanup_preserves_operation_id(self, run_command):
        operation_id = volumes.create_operation(
            "fr-par-2", {"type": "RENDER-S", "volume": {"size_gb": 50}}
        )
        volume_id = "12345678-1234-4234-8234-123456789abc"
        volumes.update_operation(
            operation_id, volume_id=volume_id, state="volume_ready"
        )
        instance = ScalewayInstance(
            type="RENDER-S",
            vcpus=8,
            ram=32,
            storage=0,
            bandwidth=1000,
            operation_id=operation_id,
            volume_id=volume_id,
        )
        with self.assertRaises(CalledProcessError):
            instance.delete()
        self.assertEqual(instance.volume_id, volume_id)
        operation = volumes.get_operation(operation_id)
        self.assertEqual(operation["state"], "volume_ready")
        self.assertEqual(operation["volume_id"], volume_id)

    @patch("models.run")
    def test_save_detaches_prefetch_volume_before_backup(self, run_command):
        run_command.side_effect = [
            Mock(
                stdout=json.dumps(
                    {
                        "state": "running",
                        "Volumes": [
                            {"id": "root-volume"},
                            {"id": "prefetch-volume"},
                        ],
                    }
                )
            ),
            Mock(),
            Mock(),
            Mock(stdout='{"id":"saved-image"}'),
        ]
        instance = ScalewayInstance(
            server_id="server-id",
            volume_id="prefetch-volume",
            vcpus=8,
            ram=32,
            storage=0,
            bandwidth=1000,
        )
        with patch.object(instance, "_record_state"):
            instance.save()
        detach_args = run_command.call_args_list[2].args[0]
        backup_args = run_command.call_args_list[3].args[0]
        self.assertIn("volume-ids.0=root-volume", detach_args)
        self.assertEqual(backup_args[3], "backup")

    def test_delete_ignores_only_not_found_and_preserves_other_failures(self):
        missing = CalledProcessError(
            1,
            "scw",
            stderr=json.dumps(
                {"message": "cannot find resource 'volume' with ID 'missing-id'"}
            ),
        )
        volumes.delete_block_volume(
            "12345678-1234-4234-8234-123456789abc",
            zone="fr-par-2",
            run_command=Mock(side_effect=missing),
        )
        denied = CalledProcessError(1, "scw", stderr="access denied")
        with self.assertRaises(CalledProcessError):
            volumes.delete_block_volume(
                "12345678-1234-4234-8234-123456789abc",
                zone="fr-par-2",
                run_command=Mock(side_effect=denied),
            )

    def test_s3_sync_uses_recursive_prefixes_without_controller_listing(self):
        run_command = Mock()
        credentials = {
            "access_key": "local-access",
            "secret_key": "local-secret",
        }
        with (
            patch.dict(os.environ, {"USER": "adrien"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            volumes.sync_prefixes_over_ssh(
                host="192.0.2.10",
                config={
                    "bucket": "model-bucket",
                    "dirs": ["checkpoints/", "loras/"],
                    "mount_path": "/mnt/models",
                    "size_gb": 100,
                    "prefetch_timeout_minutes": 10,
                    "ssh_user": "root",
                    "ssh_known_hosts_file": "/tmp/op-known-hosts",
                },
                credentials=credentials,
                region="fr-par",
                run_command=run_command,
            )
        command = run_command.call_args.args[0]
        remote_script = run_command.call_args.kwargs["input"]
        self.assertIn("s3://model-bucket/users/adrien/checkpoints/", remote_script)
        self.assertIn("s3://model-bucket/users/adrien/loras/", remote_script)
        self.assertIn("/mnt/models/checkpoints", remote_script)
        self.assertIn("/mnt/models/loras", remote_script)
        self.assertEqual(remote_script.count("aws s3 sync"), 2)
        self.assertNotIn("list-objects", remote_script)
        self.assertIn("local-secret", remote_script)
        self.assertNotIn("local-secret", command)
        self.assertIn("UserKnownHostsFile=/tmp/op-known-hosts", command)
        self.assertIn("root@192.0.2.10", command)
        self.assertIn(
            "s3://model-bucket/users/adrien/checkpoints/ -> /mnt/models/checkpoints",
            output.getvalue(),
        )
        self.assertIn("S3 sync and volume capacity check completed", output.getvalue())

    def test_helper_cloud_init_guards_root_disk_before_formatting(self):
        cloud_init = volumes.helper_cloud_init("/mnt/models")
        script = yaml.safe_load(cloud_init)["runcmd"][0][-1]
        self.assertIn("Expected exactly one non-root data disk", script)
        self.assertIn(
            "Refusing to format a data disk that already has a filesystem", script
        )
        self.assertIn('mkfs.ext4 -F "$data_disk"', script)
        self.assertIn("/mnt/models", script)

    def test_empty_s3_prefix_and_capacity_overflow_fail_in_helper_script(self):
        run_command = Mock()
        volumes.sync_prefixes_over_ssh(
            host="192.0.2.10",
            config={
                "bucket": "model-bucket",
                "dirs": ["checkpoints/"],
                "mount_path": "/mnt/models",
                "size_gb": 5,
                "ssh_user": "root",
            },
            credentials={"access_key": "a", "secret_key": "b"},
            region="fr-par",
            run_command=run_command,
        )
        script = run_command.call_args.kwargs["input"]
        self.assertIn("Requested S3 prefix is empty", script)
        self.assertIn("Synced data exceeds the configured volume size", script)

    def test_worker_volume_builder_preserves_object_and_prefix_hierarchy(self):
        script_path = Path(__file__).parents[2] / "prefetch-volume.sh"
        script = script_path.read_text()
        self.assertIn("command -v aws >/dev/null", script)
        self.assertIn("AWS CLI is required on the worker", script)
        self.assertIn('destination="$mount_path/$prefix"', script)
        self.assertIn("aws s3api head-object", script)
        self.assertIn('aws s3 cp "s3://$bucket/$object_key" "$destination"', script)
        self.assertIn('aws s3 sync "$source" "$destination"', script)
        self.assertIn('"$component" != . && "$component" != ..', script)
        self.assertNotIn('"$prefix" != *..*', script)
        self.assertIn(
            'attach-volume "server-id=$worker" "volume-id=$volume" volume-type=sbs_volume',
            script,
        )
        self.assertEqual(
            script.count('detach-volume "server-id=$worker" "volume-id=$volume"'), 2
        )
        self.assertIn("volume_attached_to_worker()", script)
        self.assertIn("run_aws_transfer()", script)
        self.assertIn("attempt <= 5", script)
        self.assertIn('run_aws_transfer aws s3 sync "$source" "$destination"', script)
        self.assertIn(
            'any(.[]; .product_resource_id == $worker and .status == "attached")',
            script,
        )
        self.assertIn(
            "Expected one new ${size_gb}GB disk after waiting 60 seconds", script
        )
        self.assertIn(
            'filesystem_type=$(sudo -n blkid -p -s TYPE -o value "$disk"', script
        )
        self.assertIn("blkid_status != 2", script)
        self.assertIn('[[ "$filesystem_type" == ext4 ]]', script)
        self.assertIn("Refusing to use $disk with unsupported filesystem", script)
        self.assertIn('sudo -n chown "$(id -u):$(id -g)" "$mount_path"', script)
        self.assertIn("awk '/^[[:space:]]*[\\[{]/ {in_json=1} in_json {print}'", script)
        self.assertIn('echo "Reusing existing volume $volume named $name"', script)
        self.assertIn("Volume may be retained; look up the volume named", script)

    @patch("models.run")
    @patch(
        "volumes.resolve_block_volume",
        return_value="12345678-1234-4234-8234-123456789abc",
    )
    @patch("volumes.verify_target_mount")
    def test_target_is_created_with_existing_volume_and_mount_cloud_init(
        self, verify_mount, resolve_volume, run_command
    ):
        run_command.side_effect = [
            Mock(stdout='{"id":"server-id"}'),
            Mock(stdout='{"state":"running"}'),
        ]
        instance = ScalewayInstance(
            type="RENDER-S",
            vcpus=8,
            ram=32,
            storage=0,
            bandwidth=1000,
            volume={
                "name": "models-prefetched",
                "mount_path": "/mnt/models",
            },
        )
        with (
            patch.object(instance, "_record_state"),
            redirect_stdout(io.StringIO()) as output,
        ):
            instance.create()
        create_command = run_command.call_args_list[0].args[0]
        self.assertIn(
            "additional-volumes.0=12345678-1234-4234-8234-123456789abc",
            create_command,
        )
        cloud_init = next(
            arg for arg in create_command if arg.startswith("cloud-init=")
        )
        self.assertIn("/mnt/models", cloud_init)
        self.assertIn(
            "/dev/disk/by-id/scsi-0SCW_sbs_volume-12345678-1234-4234-8234-123456789abc",
            cloud_init,
        )
        self.assertIn('[[ -b "$data_disk" ]] && break', cloud_init)
        self.assertNotIn("Expected one attached non-root data disk", cloud_init)
        self.assertNotIn("mkfs", cloud_init)
        verify_mount.assert_called_once()
        resolve_volume.assert_called_once_with(
            volume_id=None,
            name="models-prefetched",
            zone="fr-par-2",
            run_command=run_command,
        )
        self.assertIn("Verifying mount /mnt/models", output.getvalue())
        self.assertIn("Mount /mnt/models verified", output.getvalue())

    @patch.dict(
        os.environ, {"SCW_ACCESS_KEY": "access", "SCW_SECRET_KEY": "secret"}, clear=True
    )
    def test_credentials_are_read_from_environment(self):
        self.assertEqual(
            volumes.credentials_from_environment(),
            {"access_key": "access", "secret_key": "secret"},
        )


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

                @patch("runner.ScalewayInstance.load")
                def test_stop_deletes_detached_volume_after_server_is_gone(self, load):
                    instance = Mock(
                        server_id=None,
                        operation_id="operation-id",
                        zone="fr-par-2",
                        dry_run=False,
                        save_on_completion=False,
                    )
                    instance.return_value = instance
                    load.return_value = instance
                    with (
                        patch("sys.argv", ["scw-stop", "server-id"]),
                        patch("runner.volumes.get_operation", return_value=None),
                        redirect_stdout(io.StringIO()),
                    ):
                        scw_stop()
                    instance.delete.assert_called_once()
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
