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
    known_args, extra_cli_flags = parser.parse_known_args()

    # Convert all arbitrary extra CLI flags into dictionary overrides
    runtime_overrides = parse_cli_overrides(extra_cli_flags)

    data = parse(known_args.workflow, **runtime_overrides)
    print("============ Parsed Workflow Data ============")
    print(json.dumps(data, indent=4))
    print("==============================================")
    task_data = {
        **data["definition"],
        **data["task"],
        "deliveries": BaseDelivery.from_list(data.get("deliveries", [])),
    }
    task: Task = Task.from_dict(task_data["type"], **task_data)
    task()


if __name__ == "__main__":
    main()
