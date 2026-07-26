import json

from experiments.swe_alignment.data import build_dpo_pairs, build_sft_records
from experiments.swe_alignment.reward import score_patch_evaluation
from experiments.swe_alignment.rollout import summarize_rollouts
from experiments.swe_alignment.schema import PatchEvaluation, SWEInstance
from scripts.prepare_swe_alignment_dataset import build_alignment_artifacts, build_parser


def test_swe_reward_prefers_safe_passing_patch() -> None:
    passing = PatchEvaluation(
        instance_id="demo__repo-1",
        patch_apply_ok=True,
        tests_pass=True,
        fail_to_pass_passed=True,
        pass_to_pass_passed=True,
        files_changed_count=1,
        diff_line_count=12,
    )
    failing = PatchEvaluation(
        instance_id="demo__repo-1",
        patch_apply_ok=True,
        tests_pass=False,
        blocked_path_violation=True,
        files_changed_count=8,
        diff_line_count=400,
    )

    assert score_patch_evaluation(passing)["reward"] > score_patch_evaluation(failing)["reward"]
    assert score_patch_evaluation(passing)["task_success"] is True
    assert score_patch_evaluation(failing)["task_success"] is False


def test_build_sft_records_from_swe_instance() -> None:
    instance = SWEInstance.from_mapping(
        {
            "instance_id": "demo__repo-1",
            "repo": "demo/repo",
            "base_commit": "abc123",
            "problem_statement": "Function crashes when input is None.",
            "patch": "diff --git a/app.py b/app.py\n+return ''\n",
            "FAIL_TO_PASS": ["tests/test_app.py::test_none"],
        }
    )

    records = build_sft_records([instance])

    assert len(records) == 1
    assert "Function crashes" in records[0]["prompt"]
    completion = json.loads(records[0]["completion"])
    assert completion["patch_text"].startswith("diff --git")


def test_dpo_pairs_and_rollout_summary() -> None:
    good = PatchEvaluation(instance_id="case-1", patch="good patch", tests_pass=True, diff_line_count=10)
    bad = PatchEvaluation(instance_id="case-1", patch="bad patch", tests_pass=False, diff_line_count=300)

    pairs = build_dpo_pairs([good, bad])
    summary = summarize_rollouts([good, bad])

    assert len(pairs) == 1
    assert pairs[0]["chosen"] == "good patch"
    assert summary["num_instances"] == 1
    assert summary["best_of_n_success_rate"] == 1.0


def test_prepare_swe_alignment_artifacts(workspace_tmp_dir) -> None:
    instances = workspace_tmp_dir / "instances.jsonl"
    rollouts = workspace_tmp_dir / "rollouts.jsonl"
    report = workspace_tmp_dir / "report.json"
    output_dir = workspace_tmp_dir / "alignment"

    instances.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "instance_id": "demo__repo-1",
                        "repo": "demo/repo",
                        "base_commit": "abc123",
                        "problem_statement": "Fix resolved case.",
                        "patch": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                    }
                ),
                json.dumps(
                    {
                        "instance_id": "demo__repo-2",
                        "repo": "demo/repo",
                        "base_commit": "def456",
                        "problem_statement": "Fix unresolved case.",
                        "patch": "diff --git a/lib.py b/lib.py\n--- a/lib.py\n+++ b/lib.py\n@@ -1 +1 @@\n-bad\n+good\n",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    rollouts.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "instance_id": "demo__repo-1",
                        "model_patch": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                        "workspace_diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                        "patch_apply_ok": True,
                        "duration_ms": 1000,
                    }
                ),
                json.dumps(
                    {
                        "instance_id": "demo__repo-2",
                        "model_patch": "",
                        "workspace_diff": "",
                        "patch_apply_ok": True,
                        "duration_ms": 2000,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    report.write_text(
        json.dumps(
            {
                "total_instances": 2,
                "submitted_instances": 1,
                "completed_instances": 1,
                "resolved_instances": 1,
                "unresolved_instances": 0,
                "error_instances": 0,
                "resolved_ids": ["demo__repo-1"],
                "error_ids": [],
            }
        ),
        encoding="utf-8",
    )

    args = build_parser().parse_args(
        [
            "--instances",
            str(instances),
            "--rollouts",
            str(rollouts),
            "--report",
            str(report),
            "--output-dir",
            str(output_dir),
        ]
    )
    result = build_alignment_artifacts(args)

    assert result["sft_gold_records"] == 2
    assert result["sft_resolved_model_records"] == 1
    assert result["dpo_pairs"] == 2
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["dataset"]["official_resolved"] == 1
    assert (output_dir / "summary.md").exists()
