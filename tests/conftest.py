"""
conftest.py — Shared fixtures and sys.path setup for all tests.
"""
import sys
from pathlib import Path

# Make src/ importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

