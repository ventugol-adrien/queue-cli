from models import Text2Image, Task, BaseDelivery, Email, Notification
from yaml_compiler import parse
from pathlib import Path

if __name__ == "__main__":
    data = parse(Path("/home/adrien/dev/utils/queue-cli/workflows/t2i.yaml"))
    print(data)
    task_data = {
        **data["definition"],
        **data["task"],
        "deliveries": data.get("deliveries", []),
    }
    task: Task = Task.from_dict(task_data["type"], **task_data)
    task()
