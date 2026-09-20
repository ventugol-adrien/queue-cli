import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from models import Email, Image2Video
from runner import main


class Image2VideoCommandTests(unittest.TestCase):
    def render_command(self, **kwargs):
        task = Image2Video(
            name="I2V",
            location="desktop",
            type="image2video",
            model="minimax_fp8",
            **kwargs,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            task(dry_run=True)
        return output.getvalue()

    def test_omits_empty_image_argument(self):
        command = self.render_command()

        self.assertNotIn("--image", command)

    def test_serializes_image_path(self):
        command = self.render_command(image=["/tmp/input.png"])

        self.assertIn("--image /tmp/input.png", command)
        self.assertNotIn("--image '[", command)


class DeliveryLoggingTests(unittest.TestCase):
    def test_attachment_logging_does_not_block_delivery(self):
        data = {
            "definition": {},
            "task": {"type": "image2video"},
            "deliveries": [
                {
                    "type": "email",
                    "to": "recipient@example.com",
                    "subject": "Complete",
                    "body": "Task finished",
                    "attachments": ["/tmp/output video.mp4"],
                }
            ],
        }
        task = Mock()
        task.model_dump.return_value = {}
        task.return_value.model_dump.return_value = {}
        output = io.StringIO()
        with (
            patch("sys.argv", ["task", "workflow.yaml"]),
            patch("runner.parse", return_value=data),
            patch("runner.Task.from_dict", return_value=task),
            patch.object(Email, "deliver", autospec=True) as deliver,
            redirect_stdout(output),
        ):
            main()

        logged_delivery = json.loads(output.getvalue().split("=" * 40 + "\n")[-2])
        self.assertEqual(logged_delivery["attachments"], ["/tmp/output video.mp4"])
        deliver.assert_called_once()
        self.assertEqual(
            deliver.call_args.args[0].attachments, [Path("/tmp/output video.mp4")]
        )


if __name__ == "__main__":
    unittest.main()
