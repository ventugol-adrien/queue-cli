from pydantic import BaseModel
from typing import List
from pydantic import Field
from pathlib import Path
import shutil
import shlex
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

    def deliver(self, *args, **kwargs) -> None:
        cmd = ["curl"]
        if self.title:
            cmd.extend(["-H", f"Title: {self.title}"])
        if self.destination:
            cmd.extend(["-H", f"X-Target: {self.destination}"])
        if self.body:
            cmd.extend(["-d", self.body, self.endpoint])

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


class Text2Image(Task):
    model: str
    prompt: str = Field(default="", alias="user_input")
    steps: int = Field(default=40)
    cfg: float = Field(default=7.0, alias="cfg_scale")
    width: int = Field(default=512)
    height: int = Field(default=512)
    seed: int = Field(default=42, alias="image_seed")
    out: Path = Field(default=Path("./output.png"))

    def __call__(
        self, cli: bool = True, output_path: Path = None, *args, **kwds
    ) -> None:
        # 1. Use the stable-diffusion cli or API to generate the image, and store it at a path.
        executable = shutil.which("stable-diffusion")
        if executable is None:
            raise FileNotFoundError("stable-diffusion CLI was not found on PATH")

        executable_path = Path(executable)
        command = [executable]
        if executable_path.resolve().suffix == ".sh":
            command.insert(0, "bash")
        for key, value in self.__dict__.items():
            if key in ["deliveries", "type", "name", "location"]:
                continue
            command.append(f"--{key}")
            command.append(str(value).lower())

        try:
            print(f"Running command: {shlex.join(command)}")
            run(command, check=True)
        except CalledProcessError as e:
            print(f"Error occurred: {e}")

        # 2. Deliver the image to the specified deliveries.
        for delivery in self.deliveries:
            delivery.deliver()
