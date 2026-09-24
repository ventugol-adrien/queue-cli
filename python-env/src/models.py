from pydantic import BaseModel
from typing import Any, Dict, List, Tuple, cast, Literal
from pydantic import Field
from pathlib import Path
import shutil
import shlex
import mimetypes
import json
import re
import sqlite3
from contextlib import closing
from typing import Optional
from subprocess import run, CalledProcessError, CompletedProcess
from jinja2 import Template
import time
from uuid import UUID


class BaseDelivery(BaseModel):
    type: str

    @classmethod
    def from_dict(cls, type: str, **kwargs) -> "BaseDelivery":
        match type:
            case "email":
                return Email(type=type, **kwargs)
            case "notification":
                return Notification(type=type, **kwargs)

    @classmethod
    def from_list(cls, deliveries: List[dict]) -> List["BaseDelivery"]:
        return [cls.from_dict(**delivery) for delivery in deliveries]

    def render(self, context: dict) -> "BaseDelivery":
        def _render_val(val: Any) -> Any:
            if isinstance(val, str):
                return Template(val).render(**context)
            elif isinstance(val, list):
                return [_render_val(item) for item in val]
            elif isinstance(val, dict):
                return {k: _render_val(v) for k, v in val.items()}
            return val

        rendered = {k: _render_val(v) for k, v in self.model_dump().items()}
        return self.__class__(**rendered)

    def deliver(*args, **kwargs) -> None:
        raise NotImplementedError("Deliver method must be implemented by subclasses")


class Email(BaseDelivery):
    to: str
    subject: str
    body: str
    attachments: List[Path] = Field(default_factory=list)

    def deliver(self, *args, **kwargs) -> None:
        cmd = ["email"]
        for key, value in self.model_dump().items():
            if key == "attachments":
                for attachment in value:
                    cmd.append(f"-a")
                    cmd.append(str(attachment))
            elif key not in ["type", "attachments"]:
                cmd.append(f"--{key}")
                cmd.append(str(value))

        print(f"Running command: {shlex.join(cmd)}")
        run(cmd, check=True)


class Notification(BaseDelivery):
    endpoint: str
    title: str
    destination: str = Field(default="")
    body: str = Field(default="")
    priority: int = Field(default=1)
    tags: List[str] = Field(default_factory=list)
    markdown: bool = Field(default=True)
    output: str = Field(default="./output.png")
    attachment: str = Field(default="")

    def deliver(self, *args, **kwargs) -> None:
        cmd = ["curl", "-X", "POST"]
        if self.title:
            cmd.extend(["-H", f"Title: {self.title}"])
        if self.destination:
            cmd.extend(["-H", f"X-Target: {self.destination}"])
        if self.output and not self.attachment:
            cmd.extend(
                [
                    "-H",
                    f"Click: {self.output}",
                ]
            )
        if self.attachment:
            content_type = (
                mimetypes.guess_type(self.attachment)[0] or "application/octet-stream"
            )
            cmd.extend(
                [
                    "-H",
                    f"Filename: {Path(self.attachment).name}",
                    "-H",
                    f"Content-Type: {content_type}",
                    "--data-binary",
                    f"@{self.attachment}",
                ]
            )
        elif self.body:
            cmd.extend(["-d", self.body])
            if self.markdown:
                cmd.extend(["-H", "Content-Type: text/markdown"])
        elif self.markdown:
            cmd.extend(["-H", "Content-Type: text/markdown"])
        cmd.append(self.endpoint)

        print(f"Running command: {shlex.join(cmd)}")
        run(cmd, check=True)


class TaskResult(BaseModel):
    success: bool
    duration: float = Field(default=0.0)
    output_paths: List[Path] = Field(default_factory=list)
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    model_config = {"arbitrary_types_allowed": True, "extra": "ignore"}
    name: str
    location: str
    type: str
    deliveries: List[BaseDelivery] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, task_type: str, **kwargs) -> "Task":
        match task_type:
            case "text2image":
                return Text2Image(**kwargs)
            case "image2image":
                return Image2Image(
                    **kwargs,
                )
            case "text2video":
                return Text2Video(
                    **kwargs,
                )
            case "image2video":
                return Image2Video(
                    **kwargs,
                )
            case "delivery":
                return Delivery(**kwargs)
            case _:
                raise ValueError(f"Unsupported task type: {task_type!r}")

    def __call__(self, dry_run: bool = False) -> TaskResult:
        start = time.perf_counter()
        try:
            result = self.execute(dry_run=dry_run)
        except Exception as exc:
            # Calls the SUBCLASS implementation of build_result!
            result = self.build_result(success=False, error=str(exc))

        result.duration = round(time.perf_counter() - start, 2)
        return result

    def execute(self, dry_run: bool = False) -> TaskResult:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement the execute method."
        )


class Delivery(Task):
    def __call__(self) -> None:
        for delivery in self.deliveries:
            delivery.deliver()


class Lora(BaseModel):
    name: str
    scale: float

    @classmethod
    def from_array(cls, array: List[dict]) -> List["Lora"]:
        return [cls(**item) for item in array]


class Text2ImageResult(TaskResult): ...


class Text2Image(Task):
    model: str
    prompt: str = Field(default="", alias="user_input")
    steps: int = Field(default=40)
    loras: List[Lora] = Field(default_factory=list)
    cfg: float = Field(default=7.0, alias="cfg_scale")
    width: int = Field(default=512)
    height: int = Field(default=512)
    seed: int = Field(default=42, alias="image_seed")
    out: Path = Field(default=Path(Path.home() / f"output_{int(time.time())}.png"))

    def build_result(
        self, success: bool = True, error: Optional[str] = None
    ) -> Text2ImageResult:
        """Single source of truth for constructing this task's result."""
        return Text2ImageResult(
            success=success,
            output_paths=[self.out] if success else [],
            error=error,
            metadata={
                "seed": self.seed,
            },
        )

    def execute(self, dry_run: bool = False) -> Text2ImageResult:
        executable = shutil.which("stable-diffusion")
        if executable is None:
            raise FileNotFoundError("stable-diffusion CLI was not found on PATH")

        executable_path = Path(executable)
        command = [executable]
        if executable_path.resolve().suffix == ".sh":
            command.insert(0, "bash")

        for key, value in self.model_dump().items():
            if key in ["deliveries", "type", "name", "location"]:
                continue
            if key == "init_image":
                for img in value:
                    print(f"Processing init image: {img}")
                    command.append(f"--{key.replace('_', '-')}")
                    command.append(str(img))
                continue
            if key == "loras":
                for lora in value:
                    command.extend(
                        shlex.split(
                            f"--lora {lora.get('name')} --lora-scale {lora.get('scale')}"
                        )
                    )
                continue
            command.append(f"--{key.replace('_', '-')}")
            command.append(str(value).lower())

        print(f"Running command: {shlex.join(command)}")
        if not dry_run:
            # If this exits non-zero, it raises CalledProcessError straight to Task.__call__
            run(command, check=True)

        # Happy path: Simply call your single factory method
        return self.build_result(success=True)


class Image2ImageResult(Text2ImageResult): ...


class Image2Image(Text2Image):
    init_image: List[Path] = Field(default_factory=list)

    def build_result(
        self, success: bool = True, error: Optional[str] = None
    ) -> Image2ImageResult:
        """Single source of truth for constructing this task's result."""
        base_result = super().build_result(success=success, error=error)
        return Image2ImageResult(**base_result.model_dump())

    def execute(self, dry_run: bool = False) -> Image2ImageResult:
        return cast(Image2ImageResult, super().execute(dry_run=dry_run))


class Text2VideoResult(TaskResult):
    size: int = Field(default=0)


class Text2Video(Task):
    model: str = Field(default="minimax_fp8")
    frames: int = Field(default=124)
    fps: int = Field(default=24)
    prompt: str = Field(default="", alias="user_input")
    steps: int = Field(default=8)
    loras: List[Lora] = Field(default_factory=list)
    cfg: float = Field(default=1.0, alias="cfg_scale")
    width: int = Field(default=352)
    height: int = Field(default=192)
    seed: int = Field(default=42, alias="image_seed")
    out: Path = Field(default_factory=Path)

    def build_result(
        self, success: bool = True, error: Optional[str] = None
    ) -> Text2VideoResult:
        """Single source of truth for constructing this task's result."""

        def _get_size() -> int:
            if self.out.exists():
                return self.out.stat().st_size
            return 0

        return Text2VideoResult(
            size=_get_size(),
            success=success,
            output_paths=[self.out] if success else [],
            error=error,
            metadata={
                "seed": self.seed,
            },
        )

    def execute(
        self,
        cli: bool = True,
        dry_run: bool = False,
        output_path: Path = None,
        *args,
        **kwds,
    ) -> Text2VideoResult:
        executable = shutil.which("stable-video")
        if executable is None:
            raise FileNotFoundError("stable-video CLI was not found on PATH")

        executable_path = Path(executable)
        command = [executable]
        if executable_path.resolve().suffix == ".sh":
            command.insert(0, "bash")
        for key, value in self.model_dump().items():
            if key in ["deliveries", "type", "name", "location"]:
                continue

            if key == "loras":
                for lora in value:
                    command.extend(
                        shlex.split(
                            f"--lora {lora.get('name')} --lora-scale {lora.get('scale')}"
                        )
                    )
                continue

            if key == "image":
                for image in value:
                    command.extend(["--image", str(image)])
                continue

            command.append(f"--{key.replace('_', '-')}")
            command.append(str(value).lower())
        print(f"Running command: {shlex.join(command)}")
        if not dry_run:
            # If this exits non-zero, it raises CalledProcessError straight to Task.__call__
            run(command, check=True)

        # Happy path: Simply call your single factory method
        return self.build_result(success=True)


class Image2VideoResult(Text2VideoResult): ...


class Image2Video(Text2Video):
    image: List[Path] = Field(default_factory=list)

    def build_result(
        self, success: bool = True, error: Optional[str] = None
    ) -> Image2VideoResult:
        """Single source of truth for constructing this task's result."""
        base_result = super().build_result(success=success, error=error)
        return Image2VideoResult(**base_result.model_dump())

    def execute(
        self,
        cli: bool = True,
        dry_run: bool = False,
        output_path: Path = None,
        *args,
        **kwds,
    ) -> Image2VideoResult:
        return cast(
            Image2VideoResult,
            super().execute(
                cli=cli,
                dry_run=dry_run,
                output_path=output_path,
                *args,
                **kwds,
            ),
        )


class RemoteInstanceRequest(BaseModel):
    model_config = {"extra": "forbid"}
    provider: Literal["scaleway"] = Field(default="scaleway")
    type: Optional[str] = None
    zone: str = Field(default="fr-par-2")
    timeout_minutes: int = Field(default=5, gt=0)
    save_on_completion: bool = Field(default=False, alias="save")
    image: str = Field(default="base", description="The image to use for the instance")
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata for the instance"
    )
    tags: List[str] = Field(default_factory=lambda: ["ephemeral", "worker"])
    gpu_memory_rng: Tuple[Optional[int], Optional[int]] = Field(
        default=(8, 160), description="Range of GPU memory required for the instance"
    )
    vcpus_rng: Tuple[Optional[int], Optional[int]] = Field(
        default=(4, 16), description="Range of vCPUs required for the instance"
    )
    ram_rng: Tuple[Optional[int], Optional[int]] = Field(
        default=(16, 64), description="Range of RAM required for the instance"
    )
    storage_rng: Tuple[Optional[int], Optional[int]] = Field(
        default=(100, 1000), description="Range of storage required for the instance"
    )
    bandwidth_rng: Tuple[Optional[int], Optional[int]] = Field(
        default=(100, 5000),
        description="Range of network bandwidth required for the instance",
    )

    @classmethod
    def from_dict(cls, data: dict) -> "RemoteInstanceRequest":
        provider = data.get("provider", "scaleway")
        match provider:
            case "scaleway":
                return ScalewayInstanceRequest(**data)
            case _:
                raise ValueError(f"Unsupported instance provider: {provider!r}")

    def select(self) -> "RemoteInstance":
        raise NotImplementedError("Subclasses must implement select")

    def __call__(self) -> "RemoteInstance":
        return self.select()


class ScalewayInstanceRequest(RemoteInstanceRequest):
    def _resolve_image(self) -> str:
        try:
            UUID(self.image)
        except ValueError:
            pass
        else:
            return self.image
        try:
            result = run(
                ["scw", "instance", "image", "list", f"zone={self.zone}", "-o", "json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "No error output from scw").strip()
            raise RuntimeError(
                f"Scaleway image lookup for {self.image!r} in {self.zone} "
                f"failed (exit {exc.returncode}): {detail}"
            ) from exc
        matches = [
            image
            for image in json.loads(result.stdout)
            if image.get("name") == self.image
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Multiple images named {self.image!r} in {self.zone}; use an image ID"
            )
        if matches:
            image = matches[0]
            if image.get("state") != "available":
                raise ValueError(
                    f"Image {self.image!r} in {self.zone} is not available"
                )
            return image["id"]
        return self.image

    def select(self) -> "ScalewayInstance":
        """Select the cheapest available type; memory is GiB, storage GB, bandwidth Mbps."""
        ranges = {
            "gpu_memory": self.gpu_memory_rng,
            "vcpus": self.vcpus_rng,
            "ram": self.ram_rng,
            "storage": self.storage_rng,
            "bandwidth": self.bandwidth_rng,
        }
        if self.type is not None:
            ranges = {
                resource: bounds
                for resource, bounds in ranges.items()
                if f"{resource}_rng" in self.model_fields_set
            }
        for resource, (lower, upper) in ranges.items():
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"{resource}_rng minimum must not exceed its maximum")

        result = run(
            [
                "scw",
                "instance",
                "server-type",
                "list",
                f"zone={self.zone}",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        candidates = []
        for server_type in json.loads(result.stdout):
            if server_type.get("availability") not in ("available", "scarce"):
                continue
            name = server_type["name"]
            if self.type is not None and name != self.type:
                continue
            gpu_count = server_type.get("gpu", 0)
            memory_match = re.search(r"-(\d+)G(?:$|-)", name)
            if not gpu_count:
                gpu_memory = 0
            elif memory_match:
                gpu_memory = int(memory_match.group(1)) * gpu_count
            elif "3070" in name:
                gpu_memory = 8 * gpu_count
            elif "RENDER" in name:
                gpu_memory = 16 * gpu_count
            else:
                continue
            resources = {
                "gpu_memory": gpu_memory,
                "vcpus": server_type["cpu"],
                "ram": server_type["ram"] / 1024**3,
                "storage": server_type["local_volume_max_size"] / 10**9,
                "bandwidth": server_type["bandwidth"] / 10**6,
            }
            if any(
                (value is None and (lower is not None or upper is not None))
                or (value is not None and lower is not None and value < lower)
                or (value is not None and upper is not None and value > upper)
                for resource, (lower, upper) in ranges.items()
                for value in (resources[resource],)
            ):
                continue
            price = server_type["hourly_price"]
            price_nanos = int(price["units"]) * 10**9 + price["nanos"]
            candidates.append(
                (price_nanos, name, resources, price.get("currency_code"))
            )

        if not candidates:
            raise ValueError(
                f"No available Scaleway instance matches the ranges in {self.zone}"
            )

        price_nanos, name, resources, currency = min(
            candidates, key=lambda candidate: candidate[:2]
        )
        return ScalewayInstance(
            provider=self.provider,
            type=name,
            hourly_price=price_nanos / 10**9,
            currency=currency,
            image=self._resolve_image(),
            zone=self.zone,
            tags=self.tags,
            timeout_minutes=self.timeout_minutes,
            save_on_completion=self.save_on_completion,
            metadata=self.metadata,
            **resources,
        )


class RemoteInstance(BaseModel):
    dry_run: bool = Field(default=False, exclude=True)
    hourly_price: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = None
    provider: Literal["scaleway"] = Field(default="scaleway")
    type: str = Field(default="RENDER-S")
    image: str = Field(default="base", description="The image to use for the instance")
    zone: str = Field(default="fr-par-2")
    timeout_minutes: int = 5
    save_on_completion: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=lambda: ["ephemeral", "worker"])
    gpu_memory: int = Field(
        default=0, description="Amount of GPU memory the instance has."
    )
    vcpus: int = Field(description="Number of vCPUs the instance has.")
    ram: int = Field(description="Amount of RAM the instance has.")
    storage: int = Field(description="Amount of storage the instance has.")
    bandwidth: int = Field(
        description="Amount of network bandwidth the instance has",
    )

    def __call__(self, dry_run: bool = False) -> "RemoteInstance":
        self.dry_run = dry_run
        return self

    def __enter__(self) -> "RemoteInstance":
        self.create()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> Literal[False]:
        try:
            if self.save_on_completion:
                self.save()
        finally:
            self.delete()
        return False

    def create(self) -> None:
        """Defines how a remote instance is created."""
        raise NotImplementedError(
            "The create method must be implemented by subclasses."
        )

    def save(self) -> None:
        raise NotImplementedError("The save method must be implemented by subclasses.")

    def delete(self) -> None:
        raise NotImplementedError(
            "The delete method must be implemented by subclasses."
        )


class ScalewayInstance(RemoteInstance):
    server_id: Optional[str] = None
    saved_image_id: Optional[str] = None
    created_at: Optional[float] = None
    stopped_at: Optional[float] = None
    terminated_at: Optional[float] = None
    uptime_seconds: Optional[float] = None
    run_cost: Optional[float] = None

    @classmethod
    def load(
        cls, server_id: Optional[str] = None, zone: Optional[str] = None
    ) -> "ScalewayInstance":
        database_path = Path.home() / ".local" / "share" / "scaleway" / "instances.db"
        if not database_path.is_file():
            raise ValueError("No recorded Scaleway instances found")
        with closing(
            sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(instances)")
            }
            if "config_json" not in columns:
                raise ValueError(
                    "Instance records predate saved configuration; cannot restore this instance"
                )
            if server_id is None:
                rows = connection.execute(
                    "SELECT * FROM instances WHERE deleted = 0 AND (? IS NULL OR zone = ?) "
                    "ORDER BY created_at IS NULL, created_at, rowid LIMIT 1",
                    (zone, zone),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM instances WHERE server_id = ? AND (? IS NULL OR zone = ?)",
                    (server_id, zone, zone),
                ).fetchall()
        if not rows:
            if server_id is None:
                raise ValueError("No active Scaleway instances found in the database")
            raise ValueError(f"No recorded Scaleway instance {server_id!r} found")
        if len(rows) > 1:
            raise ValueError(f"Multiple records for {server_id!r}; specify --zone")
        record = dict(rows[0])
        if record["deleted"]:
            raise ValueError(f"Scaleway instance {server_id!r} is already terminated")
        if not record["config_json"]:
            raise ValueError(f"Instance {server_id!r} has no saved configuration")
        configuration = json.loads(record["config_json"])
        for field in (
            "server_id",
            "zone",
            "saved_image_id",
            "created_at",
            "stopped_at",
            "terminated_at",
            "uptime_seconds",
            "hourly_price",
            "currency",
            "run_cost",
        ):
            configuration[field] = record[field]
        return cls.model_validate(configuration)

    def _record_state(self, deleted: bool = False) -> None:
        if self.dry_run:
            return
        if not self.server_id:
            raise ValueError("Cannot record an instance without a server_id")
        database_path = (
            Path.home() / ".local" / "share" / self.provider / "instances.db"
        )
        database_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path = Path.home() / ".local" / "state" / self.provider / "instances.db"
        if not database_path.exists() and legacy_path.is_file():
            with closing(sqlite3.connect(legacy_path)) as source:
                with closing(sqlite3.connect(database_path)) as destination:
                    source.backup(destination)
        with closing(sqlite3.connect(database_path)) as connection:
            with connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS instances (
                        zone TEXT NOT NULL,
                        server_id TEXT NOT NULL,
                        saved_image_id TEXT,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (zone, server_id)
                    )""")
                columns = {
                    "created_at": "REAL",
                    "stopped_at": "REAL",
                    "terminated_at": "REAL",
                    "uptime_seconds": "REAL",
                    "hourly_price": "REAL",
                    "currency": "TEXT",
                    "run_cost": "REAL",
                    "config_json": "TEXT",
                }
                existing_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(instances)")
                }
                for column, column_type in columns.items():
                    if column not in existing_columns:
                        connection.execute(
                            f"ALTER TABLE instances ADD COLUMN {column} {column_type}"
                        )
                previous = connection.execute(
                    "SELECT created_at, stopped_at, terminated_at, hourly_price, currency "
                    "FROM instances WHERE zone = ? AND server_id = ?",
                    (self.zone, self.server_id),
                ).fetchone()
                if previous:
                    for field, value in zip(
                        (
                            "created_at",
                            "stopped_at",
                            "terminated_at",
                            "hourly_price",
                            "currency",
                        ),
                        previous,
                    ):
                        if getattr(self, field) is None:
                            setattr(self, field, value)
                        elif field == "stopped_at" and value is not None:
                            self.stopped_at = min(self.stopped_at, value)
                if deleted and self.terminated_at is None:
                    self.terminated_at = time.time()
                if self.created_at is not None:
                    end = (
                        self.stopped_at
                        if self.stopped_at is not None
                        else self.terminated_at
                    )
                    if end is None:
                        end = time.time()
                    self.uptime_seconds = max(0.0, end - self.created_at)
                    if self.hourly_price is not None:
                        self.run_cost = self.uptime_seconds * self.hourly_price / 3600
                connection.execute(
                    """INSERT INTO instances (zone, server_id, saved_image_id, deleted)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (zone, server_id) DO UPDATE SET
                        saved_image_id = COALESCE(excluded.saved_image_id, instances.saved_image_id),
                        deleted = excluded.deleted""",
                    (self.zone, self.server_id, self.saved_image_id, int(deleted)),
                )
                connection.execute(
                    """UPDATE instances SET created_at = ?, stopped_at = ?, terminated_at = ?,
                    uptime_seconds = ?, hourly_price = ?, currency = ?, run_cost = ?, config_json = ?
                    WHERE zone = ? AND server_id = ?""",
                    (
                        self.created_at,
                        self.stopped_at,
                        self.terminated_at,
                        self.uptime_seconds,
                        self.hourly_price,
                        self.currency,
                        self.run_cost,
                        self.model_dump_json(),
                        self.zone,
                        self.server_id,
                    ),
                )

    def create(self) -> None:
        """Create and start a server, retaining its ID if waiting fails."""
        if self.server_id:
            raise ValueError("This instance already has a server_id")
        if self.timeout_minutes <= 0:
            raise ValueError("timeout_minutes must be positive")
        command = [
            "scw",
            "instance",
            "server",
            "create",
            f"type={self.type}",
            f"image={self.image}",
            "ip=new",
        ]
        command.extend(f"tags.{index}={tag}" for index, tag in enumerate(self.tags))
        command.extend([f"zone={self.zone}", "-o", "json"])
        if self.dry_run:
            print(f"[dry-run] {shlex.join(command)}")
            wait_command = [
                "scw",
                "instance",
                "server",
                "wait",
                "<server-id>",
                f"timeout={self.timeout_minutes}m",
                f"zone={self.zone}",
                "-o",
                "json",
            ]
            print(f"[dry-run] {shlex.join(wait_command)}")
            return
        started_at = time.time()
        try:
            result = run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_minutes * 60,
            )
        except CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "No error output from scw").strip()
            raise RuntimeError(
                f"Scaleway server creation failed (exit {exc.returncode}): {detail}"
            ) from exc
        server = json.loads(result.stdout)
        if not server.get("id"):
            raise ValueError("Scaleway create response did not contain a server ID")
        self.server_id = server["id"]
        self.created_at = started_at
        self.stopped_at = None
        self.terminated_at = None
        self.uptime_seconds = None
        self.run_cost = None
        self._record_state()
        result = run(
            [
                "scw",
                "instance",
                "server",
                "wait",
                self.server_id,
                f"timeout={self.timeout_minutes}m",
                f"zone={self.zone}",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_minutes * 60,
        )
        if json.loads(result.stdout).get("state") != "running":
            raise RuntimeError(
                f"Scaleway server {self.server_id} did not reach running state"
            )

    def save(self) -> None:
        """Stop the server and preserve its disks as a reusable image."""
        if not self.server_id and not self.dry_run:
            raise ValueError("Cannot save an instance without a server_id")
        if self.timeout_minutes <= 0:
            raise ValueError("timeout_minutes must be positive")
        if self.dry_run:
            server_id = self.server_id or "<server-id>"
            get_command = [
                "scw",
                "instance",
                "server",
                "get",
                server_id,
                f"zone={self.zone}",
                "-o",
                "json",
            ]
            stop_command = [
                "scw",
                "instance",
                "server",
                "stop",
                server_id,
                "--wait",
                f"zone={self.zone}",
                "-o",
                "json",
            ]
            backup_command = [
                "scw",
                "instance",
                "server",
                "backup",
                server_id,
                "--wait",
                f"zone={self.zone}",
                "-o",
                "json",
            ]
            print(f"[dry-run] {shlex.join(get_command)}")
            print(f"[dry-run, if not stopped] {shlex.join(stop_command)}")
            print(f"[dry-run] {shlex.join(backup_command)}")
            return
        result = run(
            [
                "scw",
                "instance",
                "server",
                "get",
                self.server_id,
                f"zone={self.zone}",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_minutes * 60,
        )
        server = json.loads(result.stdout)
        if server.get("state") != "stopped":
            run(
                [
                    "scw",
                    "instance",
                    "server",
                    "stop",
                    self.server_id,
                    "--wait",
                    f"zone={self.zone}",
                    "-o",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_minutes * 60,
            )
        if self.stopped_at is None:
            self.stopped_at = time.time()
        self._record_state()
        result = run(
            [
                "scw",
                "instance",
                "server",
                "backup",
                self.server_id,
                "--wait",
                f"zone={self.zone}",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_minutes * 60,
        )
        image = json.loads(result.stdout)
        if not image.get("id"):
            raise ValueError("Scaleway backup response did not contain an image ID")
        self.saved_image_id = image["id"]
        self._record_state()

    def delete(self) -> None:
        """Delete the server, its volumes and IP, but retain saved images."""
        if not self.server_id and not self.dry_run:
            return
        if self.timeout_minutes <= 0:
            raise ValueError("timeout_minutes must be positive")
        command = [
            "scw",
            "instance",
            "server",
            "terminate",
            self.server_id or "<server-id>",
            "with-block=true",
            "with-ip=true",
            "--wait",
            f"zone={self.zone}",
            "-o",
            "json",
        ]
        if self.dry_run:
            print(f"[dry-run] {shlex.join(command)}")
            return
        run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_minutes * 60,
        )
        self._record_state(deleted=True)
        uptime = (
            f"{self.uptime_seconds:.2f}s"
            if self.uptime_seconds is not None
            else "unavailable"
        )
        cost = (
            f"{self.run_cost:.6f} {self.currency or '(currency unknown)'}"
            if self.run_cost is not None
            else "unavailable"
        )
        print(
            f"Server {self.server_id} terminated. Uptime: {uptime}. Estimated compute cost: {cost}"
        )
        self.server_id = None
