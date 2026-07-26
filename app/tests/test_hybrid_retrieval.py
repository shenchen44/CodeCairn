from pathlib import Path

from app.services.retrieval.hybrid import HybridCodeRetriever, tokenize


def test_tokenize_splits_snake_case_and_camel_case() -> None:
    tokens = tokenize("format_display_name DisplayFormatter")

    assert {"format_display_name", "format", "display", "name"} <= set(tokens)
    assert {"displayformatter", "formatter"} <= set(tokens)


def test_hybrid_retrieval_ranks_symbol_definition_first() -> None:
    repo_path = Path(__file__).parent / "fixtures" / "toy_repo"
    retriever = HybridCodeRetriever(repo_path)

    result = retriever.search(
        "format_display_name crashes when name is None",
        limit=3,
    )

    assert result["indexed_files"] >= 2
    assert result["hits"][0]["path"] == "app/display.py"
    assert "bm25" in result["hits"][0]["channels"]
    assert "symbol" in result["hits"][0]["channels"]
    assert "format_display_name" in result["hits"][0]["symbols"]


def test_hybrid_retrieval_expands_to_dependent_test() -> None:
    repo_path = Path(__file__).parent / "fixtures" / "toy_repo"
    retriever = HybridCodeRetriever(repo_path)

    result = retriever.search("format_display_name", limit=5)
    hits = {item["path"]: item for item in result["hits"]}

    assert "tests/test_display.py" in hits
    assert "dependency_graph" in hits["tests/test_display.py"]["channels"]
