import io
import unittest
from contextlib import redirect_stdout

from models import Image2Video


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


if __name__ == "__main__":
    unittest.main()
