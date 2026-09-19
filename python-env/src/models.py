from pydantic import BaseModel
from typing import List
from pydantic import Field
from pathlib import Path
import shutil
import shlex
import mimetypes
from typing import Optional
from subprocess import run, CalledProcessError, CompletedProcess


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


class Text2Image(Task):
    model: str
    prompt: str = Field(default="", alias="user_input")
    steps: int = Field(default=40)
    loras: List[Lora] = Field(default_factory=list)
    cfg: float = Field(default=7.0, alias="cfg_scale")
    width: int = Field(default=512)
    height: int = Field(default=512)
    seed: int = Field(default=42, alias="image_seed")
    out: Path = Field(default=Path("./output.png"))

    def __call__(
        self,
        cli: bool = True,
        dry_run: bool = False,
        output_path: Path = None,
        *args,
        **kwds,
    ) -> None:
        # 1. Use the stable-diffusion cli or API to generate the image, and store it at a path.
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

        try:
            print(f"Running command: {shlex.join(command)}")
            if not dry_run:
                run(command, check=True)
        except CalledProcessError as e:
            print(f"Error occurred: {e}")

        # 2. Deliver the image to the specified deliveries.
        for delivery in self.deliveries:
            if not dry_run:
                delivery.deliver()


class Image2Image(Text2Image):
    init_image: List[Path] = Field(default_factory=list)
    strength: float = Field(default=0.95)


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
    out: Path = Field(default=Path("./output.png"))

    def __call__(
        self,
        cli: bool = True,
        dry_run: bool = False,
        output_path: Path = None,
        *args,
        **kwds,
    ) -> None:
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
        try:
            print(f"Running command: {shlex.join(command)}")
            if not dry_run:
                run(command, check=True)
        except CalledProcessError as e:
            print(f"Error occurred: {e}")

        # 2. Deliver the image to the specified deliveries.
        for delivery in self.deliveries:
            if not dry_run:
                delivery.deliver()


class Image2Video(Text2Video):
    image: List[Path] = Field(default_factory=list)
