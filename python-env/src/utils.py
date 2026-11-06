from pathlib import Path
import shutil


def resolve_bin(bin_name: str) -> str:
    """Resolve an executable on PATH."""
    binary = shutil.which(bin_name)
    if binary is None:
        raise FileNotFoundError(f"{bin_name} CLI was not found on PATH")
    return str(Path(binary).resolve())