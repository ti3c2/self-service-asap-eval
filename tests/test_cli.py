from pathlib import Path

import pytest

from asap_eval.cli import _load_config, _require_dataset_path, _select_samples, build_parser
from asap_eval.config import EvalConfig


def test_select_samples_non_positive_values_keep_full_dataset():
    samples = ["sample-1", "sample-2", "sample-3"]

    assert _select_samples(samples, None) == samples
    assert _select_samples(samples, 0) == samples
    assert _select_samples(samples, -10) == samples


def test_select_samples_positive_value_limits_dataset():
    samples = ["sample-1", "sample-2", "sample-3"]

    assert _select_samples(samples, 2) == ["sample-1", "sample-2"]


def test_dataset_path_argument_overrides_config(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
dataset_path = "data/default.csv"
output_dir = "results"
""",
        encoding="utf-8",
    )

    args = build_parser().parse_args(
        [
            "run",
            "--config",
            str(config_path),
            "--dataset-path",
            "data/override.csv",
        ]
    )

    config = _load_config(args)

    assert config.dataset_path == Path("data/override.csv")


def test_dataset_path_argument_supplies_missing_config_value(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
output_dir = "results"
""",
        encoding="utf-8",
    )

    args = build_parser().parse_args(
        [
            "run",
            "--config",
            str(config_path),
            "--dataset-path",
            "data/override.csv",
        ]
    )

    config = _load_config(args)

    assert config.dataset_path == Path("data/override.csv")


def test_missing_dataset_path_errors_when_required():
    with pytest.raises(ValueError, match="dataset_path must be set"):
        _require_dataset_path(EvalConfig())
