from asap_eval.cli import _select_samples


def test_select_samples_non_positive_values_keep_full_dataset():
    samples = ["sample-1", "sample-2", "sample-3"]

    assert _select_samples(samples, None) == samples
    assert _select_samples(samples, 0) == samples
    assert _select_samples(samples, -10) == samples


def test_select_samples_positive_value_limits_dataset():
    samples = ["sample-1", "sample-2", "sample-3"]

    assert _select_samples(samples, 2) == ["sample-1", "sample-2"]
