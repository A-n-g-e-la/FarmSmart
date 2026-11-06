"""Shared, checked-in prototype options; no network or database lookup."""

import json
from pathlib import Path


PLOT_OPTIONS = json.loads((Path(__file__).resolve().parents[1] / "data" / "plot_options.json").read_text(encoding="utf-8"))
