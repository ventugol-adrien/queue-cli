import json
import os
import shlex
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from pathlib import PurePosixPath
from typing import Any, Callable, Optional
from uuid import uuid4

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

RunCommand = Callable[..., subprocess.CompletedProcess]


class VolumeBuildDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    bucket: str = Field(min_length=1)
    prefixes: list[str] = Field(
        min_length=1,
        validation_alias=AliasChoices("prefixes", "dirs"),
    )
    size_gb: int = Field(ge=5, le=15000)
    zone: str = "fr-par-2"
    region: Optional[str] = None
    user: str = Field(default_factory=lambda: os.environ.get("USER", "adrien"))

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            raise ValueError(
                "volume name may contain only letters, digits, dot, underscore, hyphen"
            )
        return value

    @field_validator("prefixes")
    @classmethod
    def validate_prefixes(cls, values: list[str]) -> list[str]:
        for prefix in values:
            components = prefix.strip("/").split("/")
            if not prefix.strip("/") or any(
                part in ("", ".", "..") for part in components
            ):
                raise ValueError(f"unsafe volume prefix: {prefix!r}")
        return [prefix.strip("/") for prefix in values]

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("volume user must be a valid username")
        return value


def database_path() -> Path:
    return Path.home() / ".local" / "share" / "scaleway" / "instances.db"


def operation_known_hosts_path(operation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9-]+", operation_id):
        raise ValueError("Invalid volume operation ID for SSH host-key storage")
    directory = Path.home() / ".local" / "share" / "scaleway" / "known_hosts"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory / operation_id


def server_known_hosts_path(server_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9-]+", server_id):
        raise ValueError("Invalid server ID for SSH host-key storage")
    directory = Path.home() / ".local" / "share" / "scaleway" / "known_hosts"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory / f"server-{server_id}"


def _ssh_options(config: dict[str, Any]) -> list[str]:
    options = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    known_hosts = config.get("ssh_known_hosts_file")
    if known_hosts:
        options.extend(
            [
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
            ]
        )
    identity = config.get("ssh_identity_file")
    if identity:
        options.extend(["-i", str(Path(identity).expanduser())])
    return options


def _ensure_operations(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS volume_operations (
            operation_id TEXT PRIMARY KEY,
            zone TEXT NOT NULL,
            volume_id TEXT,
            volume_uuid TEXT,
            helper_server_id TEXT,
            target_server_id TEXT,
            state TEXT NOT NULL,
            config_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            error TEXT
        )""")
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(volume_operations)")
    }
    if "volume_uuid" not in columns:
        connection.execute("ALTER TABLE volume_operations ADD COLUMN volume_uuid TEXT")


def create_operation(zone: str, config: dict[str, Any]) -> str:
    operation_id = str(uuid4())
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        _ensure_operations(connection)
        connection.execute(
            """INSERT INTO volume_operations
            (operation_id, zone, state, config_json, created_at, updated_at)
            VALUES (?, ?, 'preparing', ?, strftime('%s', 'now'), strftime('%s', 'now'))""",
            (operation_id, zone, json.dumps(config, sort_keys=True)),
        )
    return operation_id


def update_operation(operation_id: str, **values: Any) -> None:
    allowed = {
        "volume_id",
        "volume_uuid",
        "helper_server_id",
        "target_server_id",
        "state",
        "error",
    }
    if not values or values.keys() - allowed:
        raise ValueError("Unsupported volume operation fields")
    assignments = ", ".join(f"{key} = ?" for key in values)
    parameters = [*values.values(), operation_id]
    path = database_path()
    with closing(sqlite3.connect(path)) as connection, connection:
        _ensure_operations(connection)
        cursor = connection.execute(
            f"UPDATE volume_operations SET {assignments}, updated_at = strftime('%s', 'now') "
            "WHERE operation_id = ?",
            parameters,
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown volume operation {operation_id!r}")


def get_operation(operation_id: str) -> Optional[dict[str, Any]]:
    path = database_path()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='volume_operations'"
        ).fetchone():
            return None
        row = connection.execute(
            "SELECT * FROM volume_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
    if row is None:
        return None
    operation = dict(row)
    operation["config"] = json.loads(operation.pop("config_json"))
    return operation


def _cli(
    command: list[str],
    *,
    dry_run: bool = False,
    run_command: RunCommand = subprocess.run,
) -> Optional[subprocess.CompletedProcess]:
    if dry_run:
        print(f"[dry-run] {' '.join(command)}")
        return None
    try:
        return run_command(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except subprocess.CalledProcessError as error:
        detail = "\n".join(
            part.strip()
            for part in (error.stderr, error.stdout)
            if isinstance(part, str) and part.strip()
        )
        if detail:
            error.add_note(f"Scaleway CLI response:\n{detail}")
        raise


def create_block_volume(
    *,
    zone: str,
    size_gb: int,
    operation_id: str,
    dry_run: bool = False,
    run_command: RunCommand = subprocess.run,
) -> Optional[str]:
    print(f"[volume] Requesting a {size_gb}GB block volume in {zone}...", flush=True)
    command = [
        "scw",
        "block",
        "volume",
        "create",
        f"name=queue-prefetch-{operation_id[:8]}",
        f"from-empty.size={size_gb}GB",
        "perf-iops=5000",
        f"tags.0=queue-prefetch-{operation_id}",
        f"zone={zone}",
        "--wait",
        "-o",
        "json",
    ]
    result = _cli(command, dry_run=dry_run, run_command=run_command)
    if result is None:
        return None
    try:
        volume_id = json.loads(result.stdout)["id"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(
            "Scaleway volume create response did not contain an ID"
        ) from exc
    print(f"[volume] Block volume created: {volume_id}.", flush=True)
    return volume_id


def _is_not_found(error: subprocess.CalledProcessError) -> bool:
    details = " ".join((error.stderr or "", error.stdout or "")).lower()
    return any(
        marker in details
        for marker in (
            "not found",
            "notfound",
            "does not exist",
            "unknown volume",
            "cannot find resource",
        )
    )


def delete_block_volume(
    volume_id: str,
    *,
    zone: str,
    dry_run: bool = False,
    run_command: RunCommand = subprocess.run,
) -> None:
    if not re.fullmatch(r"[0-9a-fA-F-]{16,}", volume_id):
        raise ValueError("Invalid Scaleway block volume ID")
    command = [
        "scw",
        "block",
        "volume",
        "delete",
        volume_id,
        f"zone={zone}",
        "-o",
        "json",
    ]
    try:
        _cli(command, dry_run=dry_run, run_command=run_command)
    except subprocess.CalledProcessError as exc:
        if not _is_not_found(exc):
            raise


def find_block_volume_by_name(
    name: str, *, zone: str, run_command: RunCommand = subprocess.run
) -> str:
    result = run_command(
        [
            "scw",
            "block",
            "volume",
            "list",
            f"name={name}",
            f"zone={zone}",
            "-o",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    listed = json.loads(result.stdout)
    if isinstance(listed, dict):
        listed = listed.get("volumes", [])
    matches = [item for item in listed if item.get("name") == name]
    if len(matches) != 1 or not matches[0].get("id"):
        raise ValueError(
            f"Expected one Scaleway volume named {name!r} in {zone}; found {len(matches)}"
        )
    return matches[0]["id"]


def resolve_block_volume(
    *,
    volume_id: Optional[str],
    name: Optional[str],
    zone: str,
    run_command: RunCommand = subprocess.run,
) -> str:
    if volume_id:
        result = run_command(
            [
                "scw",
                "block",
                "volume",
                "get",
                volume_id,
                f"zone={zone}",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        volume = json.loads(result.stdout)
        if volume.get("id") != volume_id:
            raise ValueError(f"Scaleway did not return volume {volume_id!r}")
        return volume_id
    if name:
        return find_block_volume_by_name(name, zone=zone, run_command=run_command)
    raise ValueError("A volume name or ID is required")


def credentials_from_environment() -> dict[str, str]:
    access_key = os.getenv("SCW_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("SCW_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
    session_token = os.getenv("SCW_SESSION_TOKEN") or os.getenv("AWS_SESSION_TOKEN")
    if not access_key or not secret_key:
        raise ValueError(
            "Set SCW_ACCESS_KEY and SCW_SECRET_KEY (or AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY) for read-only Object Storage access"
        )
    credentials = {"access_key": access_key, "secret_key": secret_key}
    if session_token:
        credentials["session_token"] = session_token
    return credentials


def region_for_zone(zone: str, configured_region: Optional[str] = None) -> str:
    region = configured_region or zone.rsplit("-", 1)[0]
    if not re.fullmatch(r"[a-z]{2}-[a-z]+", region):
        raise ValueError(f"Invalid Scaleway Object Storage region {region!r}")
    return region


def helper_cloud_init(mount_path: str) -> str:
    prepare_disk = r"""#!/bin/bash
set -euo pipefail
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y awscli
root_source=$(findmnt -n -o SOURCE /)
root_disk=$(lsblk -no PKNAME "$root_source" | head -n1)
if [ -z "$root_disk" ]; then root_disk=$(basename "$root_source"); fi
mapfile -t data_disks < <(lsblk -dnpo NAME,TYPE | awk '$2 == "disk" {print $1}' | while read -r disk; do
  [ "$(basename "$disk")" = "$root_disk" ] || printf '%s\n' "$disk"
done)
if [ "${#data_disks[@]}" -ne 1 ]; then
  echo 'Expected exactly one non-root data disk' >&2
  exit 1
fi
data_disk=${data_disks[0]}
if blkid "$data_disk" >/dev/null 2>&1; then
  echo 'Refusing to format a data disk that already has a filesystem' >&2
  exit 1
fi
mkfs.ext4 -F "$data_disk"
mount_path=__MOUNT_PATH__
mkdir -p "$mount_path"
mount "$data_disk" "$mount_path"
uuid=$(blkid -s UUID -o value "$data_disk")
printf 'UUID=%s %s ext4 defaults,nofail 0 2\n' "$uuid" "$mount_path" >> /etc/fstab
"""
    prepare_disk = prepare_disk.replace("__MOUNT_PATH__", shlex.quote(mount_path))
    return (
        "#cloud-config\nruncmd:\n  - [ bash, -lc, " + json.dumps(prepare_disk) + " ]\n"
    )


def mount_cloud_init(
    config: dict[str, Any], volume_uuid: str, supplied: Optional[dict[str, Any]] = None
) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{16,}", volume_uuid):
        raise ValueError("Invalid filesystem UUID returned by the prefetch helper")
    cloud = dict(supplied or {})
    mounts = list(cloud.get("mounts", []))
    path = config["mount_path"]
    if any(len(item) > 1 and item[1] == path for item in mounts):
        raise ValueError(f"cloud-init already configures mount path {path!r}")
    mounts.append([f"UUID={volume_uuid}", path, "ext4", "defaults,nofail", "0", "2"])
    cloud["mounts"] = mounts
    return "#cloud-config\n" + yaml.safe_dump(cloud, sort_keys=False)


def mount_existing_volume_cloud_init(
    mount_path: str, volume_id: str, supplied: Optional[dict[str, Any]] = None
) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]{16,}", volume_id):
        raise ValueError("Invalid Scaleway block volume ID")
    script = r"""set -euo pipefail
data_disk=__DATA_DISK__
for ((attempt = 0; attempt < 30; attempt++)); do
    [[ -b "$data_disk" ]] && break
    sleep 1
done
if [[ ! -b "$data_disk" ]]; then
    echo "Attached volume device not found: $data_disk" >&2
    exit 1
fi
uuid=$(blkid -s UUID -o value "$data_disk")
if [[ -z "$uuid" ]]; then echo 'Attached data volume is not formatted' >&2; exit 1; fi
mount_path=__MOUNT_PATH__
mkdir -p "$mount_path"
grep -q "^UUID=$uuid " /etc/fstab || printf 'UUID=%s %s ext4 defaults,nofail 0 2\n' "$uuid" "$mount_path" >> /etc/fstab
mountpoint -q "$mount_path" || mount "$mount_path"
"""
    script = script.replace("__MOUNT_PATH__", shlex.quote(mount_path))
    data_disk = f"/dev/disk/by-id/scsi-0SCW_sbs_volume-{volume_id}"
    script = script.replace("__DATA_DISK__", shlex.quote(data_disk))
    cloud = dict(supplied or {})
    commands = list(cloud.get("runcmd", []))
    commands.append(["bash", "-lc", script])
    cloud["runcmd"] = commands
    return "#cloud-config\n" + yaml.safe_dump(cloud, sort_keys=False)


def sync_prefixes_over_ssh(
    *,
    host: str,
    config: dict[str, Any],
    credentials: dict[str, str],
    region: str,
    run_command: RunCommand = subprocess.run,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("Invalid helper host")
    mount_path = config["mount_path"]
    bucket = config["bucket"]
    user = os.environ.get("USER", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", user) or user in (".", ".."):
        raise ValueError("USER must be set to a safe username for S3 prefix lookup")
    endpoint = f"https://s3.{region}.scw.cloud"
    lines = ["set -euo pipefail", "set +x"]
    for key, variable in (
        ("access_key", "AWS_ACCESS_KEY_ID"),
        ("secret_key", "AWS_SECRET_ACCESS_KEY"),
        ("session_token", "AWS_SESSION_TOKEN"),
    ):
        if key in credentials:
            lines.append(f"export {variable}={shlex.quote(credentials[key])}")
    for raw_prefix in config["dirs"]:
        prefix = raw_prefix.strip("/")
        destination = str(PurePosixPath(mount_path) / prefix)
        source = f"s3://{bucket}/users/{user}/{prefix}/"
        print(f"[volume] Syncing {source} -> {destination}...", flush=True)
        lines.extend(
            [
                f"mkdir -p {shlex.quote(destination)}",
                "aws s3 sync "
                f"{shlex.quote(source)} {shlex.quote(destination)} "
                f"--endpoint-url {shlex.quote(endpoint)} --region {shlex.quote(region)}",
                f"find {shlex.quote(destination)} -type f -print -quit | grep -q . || "
                f"{{ echo {shlex.quote('Requested S3 prefix is empty: ' + prefix)} >&2; exit 42; }}",
            ]
        )
    lines.extend(
        [
            f"used=$(du -s --block-size=1 {shlex.quote(mount_path)} | cut -f1)",
            f"limit={int(config['size_gb']) * 1_000_000_000}",
            'if [ "$used" -gt "$limit" ]; then '
            "echo 'Synced data exceeds the configured volume size' >&2; exit 43; fi",
            "sync",
        ]
    )
    command = ["ssh", *_ssh_options(config)]
    command.extend(
        [
            f"{config.get('ssh_user', 'ubuntu')}@{host}",
            "bash",
            "-s",
        ]
    )
    try:
        print("[volume] Waiting for S3 sync and volume capacity check...", flush=True)
        run_command(
            command,
            input="\n".join(lines) + "\n",
            check=True,
            capture_output=True,
            text=True,
            timeout=int(config.get("prefetch_timeout_minutes", 60)) * 60,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError("S3 prefix sync failed on the prefetch helper") from None
    print("[volume] S3 sync and volume capacity check completed.", flush=True)


def get_filesystem_uuid(
    *,
    host: str,
    config: dict[str, Any],
    run_command: RunCommand = subprocess.run,
) -> str:
    command = ["ssh", *_ssh_options(config)]
    script = r"""set -euo pipefail
root_source=$(findmnt -n -o SOURCE /)
root_disk=$(lsblk -no PKNAME "$root_source" | head -n1)
if [ -z "$root_disk" ]; then root_disk=$(basename "$root_source"); fi
mapfile -t data_disks < <(lsblk -dnpo NAME,TYPE | awk '$2 == "disk" {print $1}' | while read -r disk; do
    [ "$(basename "$disk")" = "$root_disk" ] || printf '%s\n' "$disk"
done)
if [ "${#data_disks[@]}" -ne 1 ]; then exit 44; fi
blkid -s UUID -o value "${data_disks[0]}"
"""
    command.extend([f"{config.get('ssh_user', 'ubuntu')}@{host}", "bash", "-s"])
    result = run_command(
        command,
        input=script,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    value = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{16,}", value):
        raise ValueError("Could not verify the prefetch volume filesystem UUID")
    return value


def select_helper_type(*, zone: str, run_command: RunCommand = subprocess.run) -> str:
    result = run_command(
        ["scw", "instance", "server-type", "list", f"zone={zone}", "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    candidates = []
    for server_type in json.loads(result.stdout):
        if server_type.get("availability") != "available" or server_type.get("gpu", 0):
            continue
        if server_type.get("cpu", 0) < 2 or server_type.get("ram", 0) < 4 * 1024**3:
            continue
        price = server_type["hourly_price"]
        price_nanos = int(price["units"]) * 10**9 + price["nanos"]
        candidates.append((price_nanos, server_type["name"]))
    if not candidates:
        raise ValueError(f"No available CPU helper instance type in {zone}")
    return min(candidates)[1]


def _server_ip(server: dict[str, Any]) -> Optional[str]:
    for public_ip in server.get("public_ips", []):
        address = public_ip.get("address") or public_ip.get("ip")
        if address:
            return address
    return None


def create_helper(
    *,
    zone: str,
    volume_id: str,
    operation_id: str,
    mount_path: str,
    timeout_minutes: int,
    dry_run: bool = False,
    run_command: RunCommand = subprocess.run,
    on_created: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[str], Optional[str]]:
    print(f"[volume] Selecting an available CPU helper in {zone}...", flush=True)
    helper_type = select_helper_type(zone=zone, run_command=run_command)
    command = [
        "scw",
        "instance",
        "server",
        "create",
        f"type={helper_type}",
        "image=ubuntu_jammy",
        "ip=new",
        f"cloud-init={helper_cloud_init(mount_path)}",
        f"additional-volumes.0={volume_id}",
        f"tags.0=queue-prefetch-{operation_id}",
        f"zone={zone}",
        "-o",
        "json",
    ]
    result = _cli(command, dry_run=dry_run, run_command=run_command)
    if result is None:
        return None, None
    try:
        helper_id = json.loads(result.stdout)["id"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(
            "Scaleway helper create response did not contain an ID"
        ) from exc
    if on_created:
        on_created(helper_id)
    print(
        f"[volume] Helper {helper_id} ({helper_type}) created; waiting for boot...",
        flush=True,
    )
    run_command(
        [
            "scw",
            "instance",
            "server",
            "wait",
            helper_id,
            f"timeout={timeout_minutes}m",
            f"zone={zone}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_minutes * 60,
    )
    result = run_command(
        ["scw", "instance", "server", "get", helper_id, f"zone={zone}", "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    server = json.loads(result.stdout)
    address = _server_ip(server)
    if not address:
        raise ValueError("Scaleway helper has no public IP for SSH prefetch")
    print(f"[volume] Helper {helper_id} is running at {address}.", flush=True)
    return helper_id, address


def wait_for_helper_cloud_init(
    *,
    host: str,
    config: dict[str, Any],
    run_command: RunCommand = subprocess.run,
) -> None:
    print("[volume] Waiting for helper cloud-init disk setup...", flush=True)
    command = ["ssh", *_ssh_options(config), "-o", "ConnectTimeout=10"]
    command.extend(
        [
            f"{config.get('ssh_user', 'ubuntu')}@{host}",
            "cloud-init status --wait",
        ]
    )
    deadline = time.monotonic() + min(
        int(config.get("prefetch_timeout_minutes", 60)) * 60, 1800
    )
    while time.monotonic() < deadline:
        try:
            run_command(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            print("[volume] Helper disk is mounted and ready.", flush=True)
            return
        except subprocess.CalledProcessError as exc:
            detail = (
                exc.stderr or exc.stdout or "SSH returned exit status 255"
            ).strip()
            transient_errors = (
                "connection refused",
                "connection timed out",
                "operation timed out",
                "no route to host",
                "network is unreachable",
                "connection reset",
            )
            if not any(marker in detail.lower() for marker in transient_errors):
                raise RuntimeError(
                    f"SSH connection to prefetch helper failed: {detail}"
                ) from exc
            print(f"[volume] SSH is not ready yet: {detail}; retrying...", flush=True)
        except subprocess.TimeoutExpired:
            print("[volume] SSH attempt timed out; retrying...", flush=True)
            time.sleep(5)
            continue
        time.sleep(5)
    raise TimeoutError("Timed out waiting for prefetch helper cloud-init")


def delete_helper(
    server_id: str,
    *,
    zone: str,
    volume_id: Optional[str] = None,
    dry_run: bool = False,
    run_command: RunCommand = subprocess.run,
) -> None:
    if not dry_run:
        try:
            server = get_server(server_id, zone=zone, run_command=run_command)
        except subprocess.CalledProcessError as exc:
            if _is_not_found(exc):
                return
            raise
        if server.get("state") != "stopped":
            run_command(
                [
                    "scw",
                    "instance",
                    "server",
                    "stop",
                    server_id,
                    "--wait",
                    f"zone={zone}",
                    "-o",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        attached = server.get("volumes") or {}
        root_ids = [
            item.get("id")
            for item in attached.values()
            if isinstance(item, dict) and item.get("id")
        ]
        if volume_id and volume_id in root_ids:
            remaining = [item for item in root_ids if item != volume_id]
            if not remaining:
                raise RuntimeError(
                    "Cannot detach the helper data volume without preserving its root volume"
                )
            run_command(
                [
                    "scw",
                    "instance",
                    "server",
                    "update",
                    server_id,
                    *(
                        f"volume-ids.{index}={volume_id}"
                        for index, volume_id in enumerate(remaining)
                    ),
                    f"zone={zone}",
                    "-o",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
    try:
        _cli(
            [
                "scw",
                "instance",
                "server",
                "terminate",
                server_id,
                "with-block=false",
                "with-ip=true",
                "--wait",
                f"zone={zone}",
                "-o",
                "json",
            ],
            dry_run=dry_run,
            run_command=run_command,
        )
    except subprocess.CalledProcessError as exc:
        if not _is_not_found(exc):
            raise


def get_server(
    server_id: str, *, zone: str, run_command: RunCommand = subprocess.run
) -> dict[str, Any]:
    result = run_command(
        ["scw", "instance", "server", "get", server_id, f"zone={zone}", "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def verify_target_mount(
    *,
    server_id: str,
    zone: str,
    config: dict[str, Any],
    timeout_minutes: int,
    run_command: RunCommand = subprocess.run,
) -> None:
    deadline = time.monotonic() + timeout_minutes * 60
    ssh_config = dict(config)
    ssh_config["ssh_known_hosts_file"] = str(server_known_hosts_path(server_id))
    while time.monotonic() < deadline:
        server = get_server(server_id, zone=zone, run_command=run_command)
        host = _server_ip(server)
        if host:
            command = [
                "ssh",
            ]
            command.extend(_ssh_options(ssh_config))
            mount_path = shlex.quote(config["mount_path"])
            check_mount = (
                f"mountpoint -q {mount_path} && "
                f'test "$(findmnt -n -o SOURCE --target {mount_path})" != '
                '"$(findmnt -n -o SOURCE /)"'
            )
            command.extend(
                [
                    f"{config.get('ssh_user', 'ubuntu')}@{host}",
                    check_mount,
                ]
            )
            try:
                run_command(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        time.sleep(5)
    raise TimeoutError("Target instance did not mount the prefetched volume")
