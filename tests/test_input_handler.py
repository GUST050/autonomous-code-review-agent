"""
Tests for utils/input_handler.py — file and git-diff reading.
No real git calls — subprocess is patched.
"""
import pytest
from unittest.mock import MagicMock, patch

from utils.input_handler import read_from_file, read_from_git_diff


class TestReadFromFile:
    def test_reads_existing_file(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def hello(): pass\n")
        code, label = read_from_file(str(f))
        assert code == "def hello(): pass\n"

    def test_label_contains_filename(self, tmp_path):
        f = tmp_path / "mymodule.py"
        f.write_text("x = 1")
        _, label = read_from_file(str(f))
        assert "mymodule.py" in label

    def test_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            read_from_file("/nonexistent/path/does_not_exist.py")

    def test_raises_on_directory(self, tmp_path):
        with pytest.raises(ValueError, match="not a file"):
            read_from_file(str(tmp_path))

    def test_raises_on_empty_file(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        with pytest.raises(ValueError, match="[Ee]mpty|empty"):
            read_from_file(str(f))

    def test_raises_on_whitespace_only_file(self, tmp_path):
        f = tmp_path / "blank.py"
        f.write_text("   \n\n   \t  ")
        with pytest.raises(ValueError):
            read_from_file(str(f))

    def test_non_empty_file_returns_full_content(self, tmp_path):
        content = "import os\n\ndef main():\n    pass\n"
        f = tmp_path / "app.py"
        f.write_text(content)
        code, _ = read_from_file(str(f))
        assert code == content


class TestReadFromGitDiff:
    def _mock_run(self, stdout, returncode=0):
        m = MagicMock()
        m.stdout = stdout
        m.returncode = returncode
        return m

    def test_returns_staged_diff_when_present(self):
        diff = "diff --git a/app.py b/app.py\n+x = 1\n"
        with patch("utils.input_handler.subprocess.run",
                   return_value=self._mock_run(diff)):
            code, label = read_from_git_diff()
        assert "diff" in code
        assert "Git diff" in label

    def test_falls_back_to_last_commit_when_working_tree_clean(self):
        last_commit_diff = "diff --git a/f.py b/f.py\n+y = 2\n"
        with patch("utils.input_handler.subprocess.run",
                   side_effect=[
                       self._mock_run(""),            # HEAD — no changes
                       self._mock_run(last_commit_diff),  # HEAD~1..HEAD
                   ]):
            code, label = read_from_git_diff()
        assert "diff" in code
        assert "last commit" in label.lower()

    def test_raises_when_no_diff_at_all(self):
        with patch("utils.input_handler.subprocess.run",
                   return_value=self._mock_run("")):
            with pytest.raises(ValueError, match="[Nn]o diff"):
                read_from_git_diff()

    def test_label_distinguishes_staged_from_last_commit(self):
        diff = "diff --git a/x.py b/x.py\n+z = 3\n"
        with patch("utils.input_handler.subprocess.run",
                   return_value=self._mock_run(diff)):
            _, label = read_from_git_diff()
        assert "HEAD" in label or "staged" in label.lower() or "unstaged" in label.lower()
