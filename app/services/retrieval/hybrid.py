from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import math
from pathlib import Path
import re


INDEXED_SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".cfg",
}
IGNORED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}
MAX_FILE_BYTES = 256_000
TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_PATTERN.findall(text):
        lowered = match.lower()
        tokens.append(lowered)
        tokens.extend(part for part in lowered.split("_") if part != lowered)
        camel_parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", match)
        tokens.extend(part.lower() for part in camel_parts if part.lower() != lowered)
    return tokens


def _module_name(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".")


@dataclass(slots=True)
class CodeDocument:
    path: str
    text: str
    tokens: list[str]
    symbols: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)


class HybridCodeRetriever:
    """Local hybrid retrieval with explainable reciprocal-rank fusion."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self.documents = self._load_documents()
        self._document_frequency: Counter[str] = Counter()
        self._term_frequency: dict[str, Counter[str]] = {}
        self._reverse_imports: dict[str, set[str]] = defaultdict(set)
        self._build_indexes()

    def _load_documents(self) -> dict[str, CodeDocument]:
        documents: dict[str, CodeDocument] = {}
        for file_path in sorted(self.repo_path.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(self.repo_path)
            if any(part in IGNORED_PARTS for part in relative.parts):
                continue
            if file_path.suffix.lower() not in INDEXED_SUFFIXES:
                continue
            try:
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            path = relative.as_posix()
            symbols, imports = self._python_metadata(path, text)
            documents[path] = CodeDocument(
                path=path,
                text=text,
                tokens=tokenize(f"{path}\n{text}"),
                symbols=symbols,
                imports=imports,
            )
        return documents

    @staticmethod
    def _python_metadata(path: str, text: str) -> tuple[set[str], set[str]]:
        if not path.endswith(".py"):
            return set(), set()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set(), set()

        symbols: set[str] = set()
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.add(node.name)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        return symbols, imports

    def _build_indexes(self) -> None:
        for path, document in self.documents.items():
            frequencies = Counter(document.tokens)
            self._term_frequency[path] = frequencies
            self._document_frequency.update(frequencies.keys())

        module_to_path = {
            _module_name(path): path
            for path in self.documents
            if path.endswith(".py")
        }
        for path, document in self.documents.items():
            for imported_module in document.imports:
                for module, imported_path in module_to_path.items():
                    if module == imported_module or module.endswith(f".{imported_module}"):
                        self._reverse_imports[imported_path].add(path)

    def _bm25_scores(self, query_tokens: list[str]) -> dict[str, float]:
        if not self.documents or not query_tokens:
            return {}
        average_length = sum(
            len(document.tokens) for document in self.documents.values()
        ) / len(self.documents)
        document_count = len(self.documents)
        scores: dict[str, float] = {}
        for path, document in self.documents.items():
            score = 0.0
            frequencies = self._term_frequency[path]
            for token in set(query_tokens):
                frequency = frequencies[token]
                if not frequency:
                    continue
                containing = self._document_frequency[token]
                inverse_frequency = math.log(
                    1 + (document_count - containing + 0.5) / (containing + 0.5)
                )
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * len(document.tokens) / max(average_length, 1)
                )
                score += inverse_frequency * frequency * 2.5 / denominator
            if score > 0:
                scores[path] = score
        return scores

    def _symbol_scores(self, query_tokens: list[str]) -> dict[str, float]:
        query = set(query_tokens)
        scores: dict[str, float] = {}
        for path, document in self.documents.items():
            symbol_tokens = {
                token
                for symbol in document.symbols
                for token in tokenize(symbol)
            }
            overlap = query & symbol_tokens
            if overlap:
                exact = sum(symbol.lower() in query for symbol in document.symbols)
                scores[path] = len(overlap) + exact * 2.0
        return scores

    def _graph_scores(self, seed_paths: list[str]) -> dict[str, float]:
        scores: dict[str, float] = defaultdict(float)
        module_to_path = {
            _module_name(path): path
            for path in self.documents
            if path.endswith(".py")
        }
        for seed_rank, seed_path in enumerate(seed_paths[:5], start=1):
            seed = self.documents[seed_path]
            boost = 1.0 / seed_rank
            for imported_module in seed.imports:
                for module, path in module_to_path.items():
                    if module == imported_module or module.endswith(
                        f".{imported_module}"
                    ):
                        scores[path] += boost
            for importing_path in self._reverse_imports.get(seed_path, set()):
                scores[importing_path] += boost
        return dict(scores)

    @staticmethod
    def _rank(scores: dict[str, float]) -> list[str]:
        return [
            path
            for path, _ in sorted(
                scores.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]

    def _snippet(self, document: CodeDocument, query_tokens: list[str]) -> dict:
        query = set(query_tokens)
        lines = document.text.splitlines()
        best_line = 1
        best_overlap = -1
        for line_number, line in enumerate(lines, start=1):
            overlap = len(query & set(tokenize(line)))
            if overlap > best_overlap:
                best_overlap = overlap
                best_line = line_number
        start = max(0, best_line - 3)
        end = min(len(lines), best_line + 2)
        return {
            "start_line": start + 1,
            "end_line": end,
            "content": "\n".join(
                f"{index + 1:4d} | {lines[index]}"
                for index in range(start, end)
            ),
        }

    def search(self, query: str, limit: int = 8) -> dict:
        query_tokens = tokenize(query)
        bm25 = self._bm25_scores(query_tokens)
        symbols = self._symbol_scores(query_tokens)
        graph = self._graph_scores(self._rank(bm25))
        channels = {
            "bm25": bm25,
            "symbol": symbols,
            "dependency_graph": graph,
        }

        fused: dict[str, float] = defaultdict(float)
        provenance: dict[str, dict] = defaultdict(dict)
        for channel_name, channel_scores in channels.items():
            for rank, path in enumerate(self._rank(channel_scores), start=1):
                fused[path] += 1.0 / (60 + rank)
                provenance[path][channel_name] = {
                    "rank": rank,
                    "raw_score": round(channel_scores[path], 6),
                }

        ranked_paths = self._rank(dict(fused))[: max(1, min(limit, 20))]
        hits = []
        for path in ranked_paths:
            document = self.documents[path]
            hits.append(
                {
                    "path": path,
                    "score": round(fused[path], 6),
                    "channels": provenance[path],
                    "symbols": sorted(document.symbols)[:20],
                    "snippet": self._snippet(document, query_tokens),
                }
            )
        return {
            "query": query,
            "hits": hits,
            "indexed_files": len(self.documents),
            "fusion": "reciprocal_rank_fusion",
        }
