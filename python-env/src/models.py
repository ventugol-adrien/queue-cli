from pydantic import BaseModel
from typing import List
from pydantic import Field
from pathlib import Path
from typing import Optional
from subprocess import run, CalledProcessError, CompletedProcess


class BaseDelivery(BaseModel):
    type: str

    @classmethod
    def from_dict(cls, type: str, **kwargs) -> "BaseDelivery":
        match type:
            case "email":
                return Email(**kwargs)
            case "notification":
                return Notification(**kwargs)

    @staticmethod
    def deliver(delivery: "BaseDelivery", file_path: Path) -> None:
        match delivery.type:
            case "email":
                print(f"Delivering {file_path} via email to {delivery.to}")
            case "notification":
                print(f"Delivering {file_path} via notification to {delivery.endpoint}")


class Email(BaseDelivery):
    to: str
    subject: str
    body: str
    attachments: List[Path] = Field(default_factory=list)


class Notification(BaseDelivery):
    endpoint: str
    title: str
    priority: str
    tags: List[str] = Field(default_factory=list)


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

    def __call__(
        self, cli: bool = True, output_path: Path = None, *args, **kwds
    ) -> None:
        # 1. Use the stable-diffusion cli or API to generate the image, and store it at a path.
        command = ["stable-diffusion"]
        for key, value in self.__dict__.items():
            if key in ["deliveries", "type", "name", "location"]:
                continue
            command.append(f"--{key}")
            if key in ["prompt", "negative_prompt"]:
                command.append(rf"\"{str(value)}\"")
            else:
                command.append(str(value).lower())
        if output_path is not None:
            command.append("-o")
            command.append(str(output_path))
        else:
            command.append("-o")
            command.append(str(Path.cwd()))

        try:
            print(f"Running command: {' '.join(command)}")
            # result: CompletedProcess = run(command, check=True)
        except CalledProcessError as e:
            print(f"Error occurred: {e}")

        # 2. Deliver the image to the specified deliveries.
        # for delivery in self.deliveries:
        #     if isinstance(delivery, Email):
        #         self._deliver_email(delivery)
        #     elif isinstance(delivery, Notification):
        #         self._deliver_notification(delivery)
