"""Make the project root importable so ``import config`` etc. works under pytest."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
