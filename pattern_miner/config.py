"""Project paths."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "workflows.csv"
BLOBS_DIR = REPO_ROOT / "workflows"
OUTPUT_DIR = REPO_ROOT / "output"
