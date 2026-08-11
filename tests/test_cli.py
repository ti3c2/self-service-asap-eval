from pathlib import Path

from asap_eval.cli import _load_config, _select_samples, build_parser


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
