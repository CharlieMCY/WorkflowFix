"""Project paths.

OUTPUT_DIR honours the DATASET_TAG env var via `common.dataset.output_dir()`
so multiple sample sizes (e.g. 10k vs 50k) can coexist under output/
without overwriting each other. BLOBS_DIR and CSV_PATH are dataset-
independent (the same workflow CSV / blob store feeds every run)."""
from pathlib import Path

from common.dataset import output_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "workflows.csv"
BLOBS_DIR = REPO_ROOT / "workflows"
OUTPUT_DIR = output_dir()
