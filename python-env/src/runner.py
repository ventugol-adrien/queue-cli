#
import argparse
import json

from models import Text2Image, Task, BaseDelivery, Email, Notification
from yaml_compiler import parse, parse_cli_overrides
from pathlib import Path


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
    deliveries = BaseDelivery.from_list(data.get("deliveries", []))
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
        if not known_args.dry_run:
            rendered.deliver()


if __name__ == "__main__":
    main()
