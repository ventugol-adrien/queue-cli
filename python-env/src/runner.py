#
import argparse
import json
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
    if path.is_file():
        template = jinja_env.from_string(path.read_text())
    else:
        template = jinja_env.get_template(args.definition)
    data = yaml.safe_load(template.render())
    if not isinstance(data, dict):
        parser.error("Instance definition must be a YAML mapping")
    try:
        request = RemoteInstanceRequest.from_dict(data)
    except ValueError as error:
        parser.error(str(error))
    instance = request()
    instance(dry_run=args.dry_run).create()
    if not args.dry_run:
        print(f"Started Scaleway server {instance.server_id} in {instance.zone}")


def _existing_scaleway_instance(description: str) -> ScalewayInstance:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "server_id",
        nargs="?",
        help="Server ID; defaults to the oldest active database record",
    )
    parser.add_argument("--zone", help="Restrict database lookup to this zone")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without changing resources or state",
    )
    args = parser.parse_args()
    try:
        instance = ScalewayInstance.load(args.server_id, args.zone)
    except ValueError as error:
        parser.error(str(error))
    print(f"Selected Scaleway server {instance.server_id} in {instance.zone}")
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
        "Terminate a recorded Scaleway instance, honoring its save setting"
    )
    try:
        if instance.save_on_completion:
            instance.save()
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
