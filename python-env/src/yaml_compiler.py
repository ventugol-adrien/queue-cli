#!/usr/bin/env python3
import argparse
import collections.abc
import json
import os
from pathlib import Path
import sys
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml

load_dotenv()

# --- Directory Paths ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = Path(
    os.getenv("TEMPLATES_DIR", str(PROJECT_ROOT / "templates"))
).resolve()
TASK_DEF_DIR = Path(
    os.getenv("TASK_DEF_DIR", str(PROJECT_ROOT / "templates/task_templates"))
).resolve()
INSTANCE_DEF_DIR = Path(
    os.getenv("INSTANCE_DEF_DIR", str(PROJECT_ROOT / "templates/instance_templates"))
).resolve()
WORKFLOWS_DIR = Path(
    os.getenv("WORKFLOWS_DIR", str(PROJECT_ROOT / "workflows"))
).resolve()
USER_WORKFLOWS_DIR = Path.home() / ".config" / "queue" / "workflows"
USER_INSTANCES_DIR = Path.home() / ".config" / "queue" / "instances"

jinja_env = Environment(
    loader=FileSystemLoader(
        [
            TEMPLATES_DIR,
            TASK_DEF_DIR,
            INSTANCE_DEF_DIR,
            *sorted(path for path in USER_INSTANCES_DIR.glob("*") if path.is_dir()),
        ]
    ),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def deep_update(source: dict, overrides: dict) -> dict:
    """Recursively merges runtime overrides into the source dictionary."""
    for key, value in overrides.items():
        if isinstance(value, collections.abc.Mapping) and key in source:
            source[key] = deep_update(source.get(key, {}), value)
        else:
            source[key] = value
    return source


def resolve_workflow_path(path_input: str | Path) -> Path:
    """Resolves workflow file from CWD or configured and user workflow directories."""
    path = Path(path_input)

    # 1. Direct path check (absolute or relative to current terminal CWD)
    if path.exists():
        return path.resolve()

    # 2. Check configured and user workflow directories
    for workflows_dir in (WORKFLOWS_DIR, USER_WORKFLOWS_DIR):
        fallback_path = workflows_dir / path.name
        if fallback_path.exists():
            return fallback_path.resolve()

    raise FileNotFoundError(
        f"Workflow file '{path_input}' not found in current directory, "
        f"{WORKFLOWS_DIR}, or {USER_WORKFLOWS_DIR}"
    )


def strip_strings(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [strip_strings(item) for item in value]
    if isinstance(value, dict):
        return {str(key).strip(): strip_strings(item) for key, item in value.items()}
    return value


def parse_cli_overrides(raw_args: list[str]) -> dict:
    """
    Parses arbitrary CLI flags into typed nested dictionaries.
    Supports:
      - Direct flags: --steps 25
            - Hyphenated flags: --user-input maps to user_input
      - Dot notation: --definition.name "My Run"
      - Booleans:     --lightning (sets True) or --lightning false
      - Types:        auto-coerces numbers, booleans, strings, and lists
    """
    overrides: dict = {}
    i = 0
    while i < len(raw_args):
        arg = raw_args[i]
        if arg.startswith("--"):
            key_raw = arg.lstrip("-").strip()

            # Handle --key=value vs --key value vs boolean flags
            if "=" in key_raw:
                key, val_raw = key_raw.split("=", 1)
                value = yaml.safe_load(val_raw)
                i += 1
            elif i + 1 < len(raw_args) and not raw_args[i + 1].startswith("--"):
                key = key_raw
                value = yaml.safe_load(raw_args[i + 1])
                i += 2
            else:
                key = key_raw
                value = True
                i += 1

            # Expand dot-notation (e.g., definition.location -> {'definition': {'location': '...'}})
            keys = [subkey.strip().replace("-", "_") for subkey in key.split(".")]
            cursor = overrides
            for subkey in keys[:-1]:
                cursor = cursor.setdefault(subkey, {})
            cursor[keys[-1]] = strip_strings(value)
        else:
            i += 1

    return overrides


def parse(workflow_file: str | Path, **overrides) -> dict:
    """Loads a job YAML spec, applies overrides, and renders the Jinja template."""
    resolved_path = resolve_workflow_path(workflow_file)

    # 1. Load the raw job spec
    with open(resolved_path, "r") as f:
        job_spec = yaml.safe_load(f) or {}

    template_name = job_spec.get("template", "base.yaml.j2")
    context = job_spec.get("values", {})

    # 2. Merge runtime Python overrides
    if overrides:
        context = deep_update(context, overrides)

    # 3. Load & render the Jinja template (resolves includes across TEMPLATES_DIR & TASK_DEF_DIR)
    template = jinja_env.get_template(template_name)
    rendered = template.render(**context)

    # 4. Parse rendered YAML into final dictionary
    return yaml.safe_load(rendered)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Compile workflow templates with dynamic CLI overrides"
    )
    parser.add_argument("workflow", help="Workflow YAML file path or name")
    known_args, extra_cli_flags = parser.parse_known_args()

    # Convert all arbitrary extra CLI flags into dictionary overrides
    runtime_overrides = parse_cli_overrides(extra_cli_flags)

    compiled_spec = parse(known_args.workflow, **runtime_overrides)
    print(json.dumps(compiled_spec, indent=4))
