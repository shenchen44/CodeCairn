import pytest

from app.services.openai.policy import POLICY_PRESETS, get_runtime_policy


def test_runtime_policy_presets_form_incremental_ablation() -> None:
    legacy = POLICY_PRESETS["legacy"]
    retrieval = POLICY_PRESETS["retrieval"]
    memory = POLICY_PRESETS["memory"]
    full = POLICY_PRESETS["full"]

    assert legacy.enable_staged_localization is False
    assert legacy.enable_hybrid_retrieval is False
    assert retrieval.enable_hybrid_retrieval is True
    assert retrieval.enable_memory is False
    assert memory.enable_memory is True
    assert memory.enable_deep_review is False
    assert full.enable_deep_review is True


def test_unknown_runtime_policy_has_actionable_error() -> None:
    with pytest.raises(ValueError, match="expected one of"):
        get_runtime_policy("unknown")
