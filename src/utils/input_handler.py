import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def read_from_file(file_path: str) -> tuple[str, str]:
    """Read a source file and return (code, label). Raises on missing or empty file."""
    path = Path(file_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    code = path.read_text(encoding="utf-8")

    if not code.strip():
        raise ValueError(f"File is empty: {file_path}")

    logger.info("Loaded %d chars from %s", len(code), path.name)
    return code, f"File: {path.name}"


def read_from_git_diff() -> tuple[str, str]:
    """
    Run git diff and return (diff, label).
    Falls back to HEAD~1..HEAD if working tree is clean.
    Raises ValueError if no diff is found at all.
    """
    project_root = Path(__file__).resolve().parent.parent.parent

    def run_diff(args: list[str]) -> tuple[str, int]:
        result = subprocess.run(
            ["git", "diff"] + args,
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip(), result.returncode

    diff, code = run_diff(["HEAD"])
    if code != 0:
        logger.warning("git diff HEAD exited with code %d", code)

    if diff:
        logger.info("Using git diff HEAD (%d chars)", len(diff))
        return diff, "Git diff (unstaged + staged vs HEAD)"

    # Clean working tree — show last commit instead
    diff, code = run_diff(["HEAD~1", "HEAD"])
    if code != 0:
        logger.warning("git diff HEAD~1 HEAD exited with code %d", code)

    if diff:
        logger.info("Working tree clean — using git diff HEAD~1..HEAD (%d chars)", len(diff))
        return diff, "Git diff (last commit)"

    raise ValueError(
        "No diff found. Make a code change and try again, "
        "or use --file to review a specific file."
    )
