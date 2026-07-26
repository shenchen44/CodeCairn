from experiments.swe_alignment.memory import (
    build_verified_memory_seed,
    recall_verified_memories,
)


def _resolved_row(instance_id: str, repo: str, root_cause: str) -> dict:
    return {
        "instance_id": instance_id,
        "repo": repo,
        "official_resolved": True,
        "localization": {
            "issue_summary": "Parser mishandles repeated delimiters",
            "root_cause_hypothesis": root_cause,
            "behavioral_contracts": ["Repeated delimiters remain distinct"],
            "candidate_files": ["src/parser.py"],
            "suspected_symbols": ["Parser.parse"],
        },
        "model_patch": "must not enter memory",
    }


def test_verified_memory_seed_only_uses_resolved_rollouts() -> None:
    rows = [
        _resolved_row("repo__project-1", "repo/project", "delimiter state"),
        {
            **_resolved_row(
                "repo__project-2",
                "repo/project",
                "wrong fix",
            ),
            "official_resolved": False,
        },
    ]

    seed = build_verified_memory_seed(rows)

    assert [item["source_instance_id"] for item in seed] == [
        "repo__project-1",
    ]
    assert "model_patch" not in seed[0]
    assert seed[0]["verification"] == "official_swebench_harness"


def test_memory_recall_is_repo_scoped_and_excludes_current_instance() -> None:
    seed = build_verified_memory_seed(
        [
            _resolved_row(
                "repo__project-1",
                "repo/project",
                "delimiter parser state",
            ),
            _resolved_row(
                "repo__project-2",
                "repo/project",
                "delimiter tokenization",
            ),
            _resolved_row(
                "other__project-3",
                "other/project",
                "delimiter parser state",
            ),
        ]
    )

    recalled = recall_verified_memories(
        seed,
        repo="repo/project",
        instance_id="repo__project-1",
        query="Fix repeated delimiter parser behavior",
    )

    assert [item["source_instance_id"] for item in recalled] == [
        "repo__project-2",
    ]
    assert recalled[0]["retrieval_score"] > 0
