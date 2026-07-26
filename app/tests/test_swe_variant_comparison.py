from scripts.compare_swe_variants import compare_runs, paired_comparison, summarize
from scripts.run_swe_instances import BenchmarkMemoryBank


def _row(instance_id: str, *, success: bool, reward: float) -> dict:
    return {
        "instance_id": instance_id,
        "status": "success" if success else "failed",
        "patch_apply_ok": success,
        "patch": "diff" if success else "",
        "swe_reward": reward,
        "model_call_count": 2,
        "tool_call_count": 3,
        "total_input_tokens": 100,
        "total_output_tokens": 20,
        "duration_ms": 500,
    }


def test_summarize_variant_rollout() -> None:
    summary = summarize(
        {
            "a": _row("a", success=True, reward=1.0),
            "b": _row("b", success=False, reward=-0.5),
        }
    )

    assert summary["success_rate"] == 0.5
    assert summary["patch_apply_rate"] == 0.5
    assert summary["no_patch_rate"] == 0.5
    assert summary["average_reward"] == 0.25
    assert summary["average_input_tokens"] == 100.0


def test_summarize_accepts_structured_reward() -> None:
    row = _row("a", success=False, reward=0.0)
    row["swe_reward"] = {"reward": 0.25, "tests_pass": False}

    summary = summarize({"a": row})

    assert summary["average_reward"] == 0.25


def test_paired_comparison_counts_regressions_and_recoveries() -> None:
    baseline = {
        "a": _row("a", success=False, reward=-0.5),
        "b": _row("b", success=True, reward=1.0),
    }
    candidate = {
        "a": _row("a", success=True, reward=1.0),
        "b": _row("b", success=False, reward=-0.5),
    }

    comparison = paired_comparison(baseline, candidate)

    assert comparison["wins"] == 1
    assert comparison["losses"] == 1
    assert comparison["ties"] == 0


def test_compare_runs_uses_first_variant_as_baseline() -> None:
    report = compare_runs(
        {
            "legacy": {"a": _row("a", success=False, reward=-0.5)},
            "full": {"a": _row("a", success=True, reward=1.0)},
        }
    )

    assert report["baseline"] == "legacy"
    assert report["paired_vs_baseline"]["full"]["wins"] == 1


def test_benchmark_memory_bank_only_stores_successful_solutions() -> None:
    bank = BenchmarkMemoryBank()
    bank.remember(
        "octo/repo",
        {
            "instance_id": "failed",
            "status": "failed",
            "summary": {"root_cause": "wrong"},
        },
    )
    bank.remember(
        "octo/repo",
        {
            "instance_id": "success",
            "status": "success",
            "summary": {"root_cause": "display None handling"},
            "localization": {
                "candidate_files": ["app/display.py"],
                "evidence": [],
                "confidence": 0.9,
            },
        },
    )

    recalled = bank.recall("octo/repo", "display None")

    assert len(recalled) == 1
    assert recalled[0]["content"]["instance_id"] == "success"
