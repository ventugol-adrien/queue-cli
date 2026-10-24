#
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import nullcontext

from models import (
    RemoteInstance,
    ScalewayInstance,
    RemoteInstanceRequest,
    Text2Image,
    Task,
    BaseDelivery,
    Email,
    Notification,
)
from yaml_compiler import jinja_env, parse, parse_cli_overrides
from pathlib import Path
import yaml
import volumes
from jinja2 import TemplateError


def _load_volume_definition(data: dict, instance_path: Path | None) -> None:
    volume_path = data.get("volume")
    if not isinstance(volume_path, str):
        return
    path = Path(volume_path).expanduser()
    if not path.is_absolute():
        base = instance_path.parent if instance_path else Path.cwd()
        path = base / path
    try:
        volume = yaml.safe_load(path.read_text())
    except OSError as error:
        raise ValueError(
            f"Could not read volume definition {str(path)!r}: {error}"
        ) from error
    if not isinstance(volume, dict):
        raise ValueError(f"Volume definition {str(path)!r} must be a YAML mapping")
    data["volume"] = volume


def scw_start():
    parser = argparse.ArgumentParser(
        description="Create a Scaleway instance from an instance definition"
    )
    parser.add_argument(
        "definition", help="Instance YAML path or filename, e.g. render-s.yaml"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview creation without provisioning"
    )
    args = parser.parse_args()
    path = Path(args.definition).expanduser()
    definition_path = path.resolve() if path.is_file() else None
    if definition_path:
        template = jinja_env.from_string(definition_path.read_text())
    else:
        template = jinja_env.get_template(args.definition)
    data = yaml.safe_load(template.render())
    if not isinstance(data, dict):
        parser.error("Instance definition must be a YAML mapping")
    try:
        _load_volume_definition(data, definition_path)
        request = RemoteInstanceRequest.from_dict(data)
    except (ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    instance = request()
    print(f"Selected instance type: {instance.type}")
    try:
        instance(dry_run=args.dry_run).create()
    except Exception:
        if instance.operation_id:
            print(
                f"Recover resources with: scw-stop --operation-id {instance.operation_id}"
            )
        raise
    if not args.dry_run:
        print(f"Started Scaleway server {instance.server_id} in {instance.zone}")
        if instance.operation_id:
            print(f"Volume operation ID: {instance.operation_id}")


def scw_volume() -> None:
    parser = argparse.ArgumentParser(
        description="Create or delete a reusable Scaleway block volume"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    create = subparsers.add_parser("create", help="Build a volume from Object Storage")
    create.add_argument("definition", help="Volume recipe YAML path or template name")
    create.add_argument("--zone", help="Override the recipe zone")
    create.add_argument("--region", help="Override the recipe Object Storage region")
    create.add_argument("--user", help="Override the recipe Object Storage user")
    create.add_argument("--worker-definition", default="volume_worker.yaml")
    create.add_argument("--ssh-alias", default="v_worker")
    create.add_argument("--timeout-minutes", type=int, default=90)
    create.add_argument("--dry-run", action="store_true")
    delete = subparsers.add_parser("delete", help="Delete a volume by name or ID")
    reference = delete.add_mutually_exclusive_group(required=True)
    reference.add_argument("--volume-name")
    reference.add_argument("--volume-id")
    delete.add_argument("--zone", default="fr-par-2")
    delete.add_argument("--yes", action="store_true", help="Confirm permanent deletion")
    args = parser.parse_args()

    if args.action == "delete":
        if not args.yes:
            parser.error("scw-volume delete is destructive; pass --yes to confirm")
        volume_id = volumes.resolve_block_volume(
            volume_id=args.volume_id,
            name=args.volume_name,
            zone=args.zone,
        )
        volumes.delete_block_volume(volume_id, zone=args.zone)
        print(f"Deleted volume {volume_id}")
        return

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.ssh_alias):
        parser.error("--ssh-alias must be a valid SSH config alias")
    definition_path = Path(args.definition).expanduser()
    if not definition_path.is_absolute():
        candidates = (
            Path.cwd() / definition_path,
            Path.home() / ".config" / "scw" / "volume_definitions" / definition_path,
            Path.home() / ".config" / "queue" / "volume_definitions" / definition_path,
        )
        definition_path = next(
            (candidate for candidate in candidates if candidate.is_file()),
            definition_path,
        )
    try:
        if definition_path.is_file():
            recipe_yaml = jinja_env.from_string(definition_path.read_text()).render()
        else:
            recipe_yaml = jinja_env.get_template(args.definition).render()
        recipe_data = yaml.safe_load(recipe_yaml)
        if not isinstance(recipe_data, dict):
            raise ValueError("Volume recipe must be a YAML mapping")
        recipe_values = volumes.VolumeBuildDefinition.model_validate(
            recipe_data
        ).model_dump()
        for field in ("zone", "region", "user"):
            override = getattr(args, field)
            if override is not None:
                recipe_values[field] = override
        recipe = volumes.VolumeBuildDefinition.model_validate(recipe_values)
        volume_name = recipe.name or f"models-{int(time.time())}"
    except (OSError, ValueError, yaml.YAMLError, TemplateError) as error:
        parser.error(f"Invalid volume recipe {args.definition!r}: {error}")

    worker_path = Path(args.worker_definition).expanduser()
    if not worker_path.is_absolute():
        candidates = (
            Path.cwd() / worker_path,
            Path.home() / ".config" / "queue" / "instances" / "scaleway" / worker_path,
        )
        worker_path = next(
            (candidate for candidate in candidates if candidate.is_file()),
            candidates[-1],
        )
    try:
        worker_data = yaml.safe_load(worker_path.read_text())
    except OSError as error:
        parser.error(f"Could not read worker definition {worker_path}: {error}")
    worker_request = RemoteInstanceRequest.from_dict(worker_data)
    if worker_request.volume is not None:
        parser.error("The volume worker definition cannot request a data volume")
    if worker_request.zone != recipe.zone:
        parser.error("The volume worker zone must match the recipe zone")
    worker = worker_request()
    region = volumes.region_for_zone(recipe.zone, recipe.region)
    if args.dry_run:
        print(f"[dry-run] create volume worker {worker.type} from {worker_path}")
        command = [
            "$HOME/.local/bin/build-volume",
            "--worker-id",
            "<worker-id>",
            "--size-gb",
            str(recipe.size_gb),
            "--bucket",
            recipe.bucket,
            "--name",
            volume_name,
            "--zone",
            recipe.zone,
            "--region",
            region,
            "--user",
            recipe.user,
        ]
        for prefix in recipe.prefixes:
            command.extend(["--prefix", prefix.rstrip("/")])
        remote_command = 'exec "$HOME/.local/bin/build-volume" ' + shlex.join(
            command[1:]
        )
        print(f"[dry-run] ssh {args.ssh_alias} {shlex.quote(remote_command)}")
        print(f"[dry-run] terminate worker; resolve volume {volume_name}")
        return

    worker.tags.append(f"scw-volume-{volume_name}")
    print(f"[volume] Creating worker {worker.type} from {worker_path}...", flush=True)
    try:
        worker.create()
        if not worker.server_id:
            raise RuntimeError("Volume worker creation returned no server ID")
        command = [
            "$HOME/.local/bin/build-volume",
            "--worker-id",
            worker.server_id,
            "--size-gb",
            str(recipe.size_gb),
            "--bucket",
            recipe.bucket,
            "--name",
            volume_name,
            "--zone",
            recipe.zone,
            "--region",
            region,
            "--user",
            recipe.user,
        ]
        for prefix in recipe.prefixes:
            command.extend(["--prefix", prefix.rstrip("/")])
        print(f"[volume] Building {volume_name} on {args.ssh_alias}...", flush=True)
        remote_command = 'exec "$HOME/.local/bin/build-volume" ' + shlex.join(
            command[1:]
        )
        try:
            subprocess.run(
                ["ssh", "-tt", args.ssh_alias, remote_command],
                check=True,
                timeout=args.timeout_minutes * 60,
            )
        except subprocess.CalledProcessError as error:
            if error.stdout:
                print(error.stdout.rstrip())
            if error.stderr:
                print(error.stderr.rstrip(), file=sys.stderr)
            try:
                retained_volume_id = volumes.find_block_volume_by_name(
                    volume_name, zone=recipe.zone
                )
                print(
                    f"Volume retained after build failure: {retained_volume_id}",
                    file=sys.stderr,
                )
            except (ValueError, subprocess.CalledProcessError, json.JSONDecodeError):
                pass
            raise RuntimeError(
                f"Remote volume builder exited with status {error.returncode}"
            ) from error
        volume_id = volumes.find_block_volume_by_name(volume_name, zone=recipe.zone)
    finally:
        if worker.server_id:
            print("[volume] Terminating volume worker...", flush=True)
            worker.delete()

    print(f"VOLUME_ID={volume_id}")
    print("Volume reference for an instance (set mount_path there):")
    print("volume:")
    print(f"  name: {volume_name}")


def _existing_scaleway_instance(
    description: str, *, allow_operation_id: bool = False
) -> ScalewayInstance:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "server_id",
        nargs="?",
        help="Server ID; defaults to the oldest active database record",
    )
    parser.add_argument("--zone", help="Restrict database lookup to this zone")
    if allow_operation_id:
        parser.add_argument(
            "--operation-id",
            help="Recover and clean up an incomplete volume-prefetch operation",
        )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without changing resources or state",
    )
    args = parser.parse_args()
    try:
        if getattr(args, "operation_id", None):
            if args.server_id:
                parser.error("Specify either server_id or --operation-id, not both")
            instance = ScalewayInstance.load_operation(args.operation_id)
            if args.zone and instance.zone != args.zone:
                parser.error("The operation does not match the requested zone")
        else:
            instance = ScalewayInstance.load(args.server_id, args.zone)
    except ValueError as error:
        parser.error(str(error))
    if instance.server_id:
        print(f"Selected Scaleway server {instance.server_id} in {instance.zone}")
    elif instance.operation_id:
        print(f"Selected volume operation {instance.operation_id} in {instance.zone}")
    print(f"Selected instance type: {instance.type}")
    return instance(dry_run=args.dry_run)


def scw_save():
    instance = _existing_scaleway_instance(
        "Stop and back up a recorded Scaleway instance as a reusable image"
    )
    instance.save()
    if not instance.dry_run:
        print(f"Saved image {instance.saved_image_id} in {instance.zone}")


def scw_stop():
    instance = _existing_scaleway_instance(
        "Terminate a recorded Scaleway instance or recover a prefetch operation",
        allow_operation_id=True,
    )
    try:
        operation = (
            volumes.get_operation(instance.operation_id)
            if instance.operation_id
            else None
        )
        if instance.save_on_completion and (
            operation is None or operation["state"] == "ready"
        ):
            instance.save()
        elif instance.save_on_completion and operation is not None:
            print("Skipping save because volume prefetch did not reach ready state")
    finally:
        instance.delete()


def main():
    parser = argparse.ArgumentParser(
        description="Compile workflow templates with dynamic CLI overrides"
    )
    parser.add_argument("workflow", help="Workflow YAML file path or name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the workflow in dry-run mode without executing deliveries",
    )
    known_args, extra_cli_flags = parser.parse_known_args()

    # Convert all arbitrary extra CLI flags into dictionary overrides
    runtime_overrides = parse_cli_overrides(extra_cli_flags)

    data = parse(known_args.workflow, **runtime_overrides)
    print("============ Parsed Workflow Data ============")
    print(json.dumps(data, indent=4))
    print("==============================================")
    deliveries = BaseDelivery.from_list(data.get("deliveries") or [])
    instance = None
    if data.get("instance") is not None:
        instance_request = RemoteInstanceRequest.from_dict(data["instance"])
        instance = instance_request()
    with (
        instance(dry_run=known_args.dry_run) if instance is not None else nullcontext()
    ):
        if data.get("task") is None:
            for delivery in deliveries:
                if not known_args.dry_run:
                    delivery.deliver()
            return
        task_data = {
            **data["definition"],
            **data["task"],
            "deliveries": deliveries,
        }
        task: Task = Task.from_dict(task_data["type"], **task_data)
        result = task(dry_run=known_args.dry_run)

        context = {
            "definition": data.get("definition", {}),
            "task": task.model_dump(exclude={"deliveries"}),
            "result": result.model_dump(),
        }

        for delivery in deliveries:
            rendered = delivery.render(context)
            print("=" * 40)
            print(rendered.model_dump_json(indent=4))
            print("=" * 40)
            if not known_args.dry_run:
                rendered.deliver()


if __name__ == "__main__":
    main()
