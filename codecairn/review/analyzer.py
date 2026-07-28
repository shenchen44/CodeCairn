from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from codecairn.review.models import (
    Assurance,
    ChangeProof,
    Claim,
    Evidence,
    EvidenceGate,
    FileChange,
    GateCoverage,
    GitSnapshot,
    GitSnapshotRevision,
    Mapping,
    PatchHunk,
    Provenance,
    RepositorySnapshot,
    Requirement,
    RequirementRevision,
    ResidualRisk,
)


HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@(?P<label>.*)$"
)


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    text: bool = True,
) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=text,
        capture_output=True,
        check=check,
    )
    return result.stdout


def _id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def requirement_contract_hash(requirements: list[Requirement]) -> str:
    """Preserve declared order while normalizing insignificant whitespace."""
    contract = [
        {
            "text": " ".join(item.text.split()),
            "category": item.category,
            "original_text": " ".join(item.original_text.split()),
        }
        for item in requirements
        if not item.deleted
    ]
    return _sha256(
        json.dumps(
            contract, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )


def resolve_base_ref(repo: Path, requested: str | None) -> str:
    if requested:
        _git(repo, "rev-parse", "--verify", f"{requested}^{{commit}}")
        return requested
    for candidate in ("main", "master", "origin/main", "origin/master"):
        if subprocess.run(
            ["git", "rev-parse", "--verify", f"{candidate}^{{commit}}"],
            cwd=repo,
            capture_output=True,
        ).returncode == 0:
            return candidate
    return "HEAD"


def _safe_file(repo: Path, relative: str) -> Path | None:
    candidate = (repo / relative).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _worktree_content(repo: Path, relative: str) -> bytes | None:
    """Read repository entries without ever dereferencing a symlink."""
    candidate = repo.resolve() / relative
    try:
        info = candidate.lstat()
    except (FileNotFoundError, OSError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return os.readlink(candidate).encode("utf-8", errors="surrogateescape")
    if not stat.S_ISREG(info.st_mode):
        return None
    return candidate.read_bytes()


def _workspace_tree(repo: Path) -> str:
    """Compatibility alias for the final, commit-independent content tree."""
    return canonical_change_identity(repo, "HEAD")[0]


def _canonical_hash(value: object) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogateescape")
    )


def _base_inventory(repo: Path, base_sha: str) -> dict[str, dict[str, str]]:
    raw = bytes(
        _git(
            repo,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            base_sha,
            text=False,
        )
    )
    inventory: dict[str, dict[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode().split(" ", 2)
        if object_type != "blob":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        content = _blob_at(repo, base_sha, path) or b""
        inventory[path] = {
            "mode": mode,
            "kind": "symlink" if mode == "120000" else "file",
            "sha256": _sha256(content),
        }
    return inventory


def _current_inventory(repo: Path) -> dict[str, dict[str, str]]:
    raw = bytes(
        _git(
            repo,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            text=False,
        )
    )
    inventory: dict[str, dict[str, str]] = {}
    root = repo.resolve()
    for path in sorted(_decode_z(raw)):
        candidate = root / path
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            # Hash the link target text; never dereference it.
            content = os.readlink(candidate).encode(
                "utf-8", errors="surrogateescape"
            )
            mode, kind = "120000", "symlink"
        elif stat.S_ISREG(info.st_mode):
            content = candidate.read_bytes()
            mode = "100755" if info.st_mode & 0o111 else "100644"
            kind = "file"
        else:
            continue
        inventory[path] = {
            "mode": mode,
            "kind": kind,
            "sha256": _sha256(content),
        }
    return inventory


def canonical_change_identity(
    repo: Path, base_ref: str
) -> tuple[str, str]:
    """Return final tree hash and deterministic base-to-final patch hash."""
    base_sha = str(_git(repo, "rev-parse", f"{base_ref}^{{commit}}")).strip()
    before = _base_inventory(repo, base_sha)
    after = _current_inventory(repo)
    tree = [
        {"path": path, **after[path]}
        for path in sorted(after, key=lambda item: item.encode(
            "utf-8", errors="surrogateescape"
        ))
    ]
    deleted = {
        path: value for path, value in before.items() if path not in after
    }
    added = {
        path: value for path, value in after.items() if path not in before
    }
    rename_pairs: dict[str, str] = {}
    available_added = set(added)
    for old_path, old_value in sorted(deleted.items()):
        matches = [
            path
            for path in available_added
            if added[path] == old_value
        ]
        if matches:
            new_path = sorted(matches)[0]
            rename_pairs[old_path] = new_path
            available_added.remove(new_path)
    changes: list[dict[str, object]] = []
    renamed_new = set(rename_pairs.values())
    for path in sorted(set(before) | set(after)):
        if path in rename_pairs:
            changes.append(
                {
                    "type": "renamed",
                    "old_path": path,
                    "path": rename_pairs[path],
                    "old": before[path],
                    "new": after[rename_pairs[path]],
                }
            )
        elif path in renamed_new:
            continue
        elif path not in before:
            changes.append({"type": "added", "path": path, "new": after[path]})
        elif path not in after:
            changes.append(
                {"type": "deleted", "path": path, "old": before[path]}
            )
        elif before[path] != after[path]:
            changes.append(
                {
                    "type": "modified",
                    "path": path,
                    "old": before[path],
                    "new": after[path],
                }
            )
    return _canonical_hash(tree), _canonical_hash(changes)


def git_snapshot_identity(
    repo: Path, base_sha: str, content_tree_hash: str
) -> str:
    head = str(_git(repo, "rev-parse", "HEAD")).strip()
    branch = str(_git(repo, "branch", "--show-current")).strip()
    status = str(_git(repo, "status", "--porcelain=v1", "-z"))
    return _id("snapshot", base_sha, head, branch, status, content_tree_hash)


def _decode_z(value: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in value.split(b"\0")
        if item
    ]


def _blob_at(repo: Path, ref: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _is_binary(content: bytes | None) -> bool:
    return bool(content is not None and b"\0" in content[:8192])


def collect_file_changes(repo: Path, base_ref: str) -> list[FileChange]:
    raw = bytes(
        _git(
            repo,
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            base_ref,
            "--",
            text=False,
        )
    )
    fields = _decode_z(raw)
    changes: list[FileChange] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        code = status[0]
        if code in {"R", "C"}:
            old_path, path = fields[index], fields[index + 1]
            index += 2
            kind = "renamed"
        else:
            path = fields[index]
            index += 1
            old_path = path if code in {"M", "D"} else None
            kind = {
                "A": "added",
                "D": "deleted",
                "M": "modified",
                "T": "modified",
            }.get(code, "modified")
        old_content = _blob_at(repo, base_ref, old_path) if old_path else None
        new_content = _worktree_content(repo, path)
        binary = _is_binary(old_content) or _is_binary(new_content)
        change_type = "binary" if binary and kind != "renamed" else kind
        changes.append(
            FileChange(
                id=_id(
                    "file",
                    change_type,
                    old_path or "",
                    path,
                    _sha256(old_content) if old_content is not None else "",
                    _sha256(new_content) if new_content is not None else "",
                ),
                change_type=change_type,
                path=path,
                old_path=old_path if old_path != path else None,
                old_content_sha256=(
                    _sha256(old_content) if old_content is not None else None
                ),
                new_content_sha256=(
                    _sha256(new_content) if new_content is not None else None
                ),
                binary=binary,
                summary=(
                    f"{change_type}: "
                    f"{old_path + ' -> ' if old_path and old_path != path else ''}"
                    f"{path}"
                )[:240],
                provenance=Provenance(kind="derived", source="git"),
            )
        )
    untracked = _decode_z(
        bytes(
            _git(
                repo,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                text=False,
            )
        )
    )
    known_paths = {item.path for item in changes}
    for path in untracked:
        if path in known_paths:
            continue
        content = _worktree_content(repo, path)
        if content is None:
            continue
        binary = _is_binary(content)
        changes.append(
            FileChange(
                id=_id("file", "added", path, _sha256(content)),
                change_type="binary" if binary else "added",
                path=path,
                new_content_sha256=_sha256(content),
                binary=binary,
                summary=f"{'binary' if binary else 'added'}: {path}"[:240],
                provenance=Provenance(kind="derived", source="git"),
            )
        )
    return changes


def build_file_comparison(
    repo: Path,
    base_ref: str,
    file_change: FileChange,
    hunks: list[PatchHunk],
) -> dict[str, object]:
    """Build aligned, full-file rows for the local side-by-side reviewer."""
    old_path = file_change.old_path or (
        file_change.path
        if file_change.change_type not in {"added"}
        else None
    )
    old_content = _blob_at(repo, base_ref, old_path) if old_path else None
    new_content = (
        _worktree_content(repo, file_change.path)
        if file_change.change_type != "deleted"
        else None
    )
    if file_change.binary:
        return {
            "file_change_id": file_change.id,
            "path": file_change.path,
            "old_path": old_path,
            "change_type": file_change.change_type,
            "binary": True,
            "rows": [],
        }

    old_lines = (
        old_content.decode("utf-8", errors="replace").splitlines()
        if old_content is not None
        else []
    )
    new_lines = (
        new_content.decode("utf-8", errors="replace").splitlines()
        if new_content is not None
        else []
    )
    relevant_hunks = [item for item in hunks if item.file_change_id == file_change.id]

    def hunk_ids(old_line: int | None, new_line: int | None) -> list[str]:
        matches: list[str] = []
        for hunk in relevant_hunks:
            old_match = (
                old_line is not None
                and hunk.old_count > 0
                and hunk.old_start <= old_line < hunk.old_start + hunk.old_count
            )
            new_match = (
                new_line is not None
                and hunk.new_count > 0
                and hunk.new_start <= new_line < hunk.new_start + hunk.new_count
            )
            if old_match or new_match:
                matches.append(hunk.id)
        return matches

    rows: list[dict[str, object]] = []

    def append_row(
        kind: str,
        old_index: int | None,
        new_index: int | None,
    ) -> None:
        old_line = old_index + 1 if old_index is not None else None
        new_line = new_index + 1 if new_index is not None else None
        rows.append(
            {
                "kind": kind,
                "old_line": old_line,
                "new_line": new_line,
                "old_text": old_lines[old_index] if old_index is not None else "",
                "new_text": new_lines[new_index] if new_index is not None else "",
                "hunk_ids": hunk_ids(old_line, new_line),
            }
        )

    def align_replacement(
        old_start: int,
        old_end: int,
        new_start: int,
        new_end: int,
    ) -> list[tuple[int | None, int | None]]:
        old_block = old_lines[old_start:old_end]
        new_block = new_lines[new_start:new_end]
        if len(old_block) * len(new_block) > 40_000:
            count = max(len(old_block), len(new_block))
            return [
                (
                    old_start + offset if offset < len(old_block) else None,
                    new_start + offset if offset < len(new_block) else None,
                )
                for offset in range(count)
            ]
        gap_cost = 0.6
        costs = [
            [0.0 for _ in range(len(new_block) + 1)]
            for _ in range(len(old_block) + 1)
        ]
        moves = [
            ["" for _ in range(len(new_block) + 1)]
            for _ in range(len(old_block) + 1)
        ]
        for old_index in range(1, len(old_block) + 1):
            costs[old_index][0] = old_index * gap_cost
            moves[old_index][0] = "delete"
        for new_index in range(1, len(new_block) + 1):
            costs[0][new_index] = new_index * gap_cost
            moves[0][new_index] = "insert"
        for old_index in range(1, len(old_block) + 1):
            for new_index in range(1, len(new_block) + 1):
                similarity = SequenceMatcher(
                    None,
                    old_block[old_index - 1].strip(),
                    new_block[new_index - 1].strip(),
                    autojunk=False,
                ).ratio()
                options = (
                    (
                        costs[old_index - 1][new_index - 1]
                        + 1.0
                        - similarity,
                        "replace",
                    ),
                    (
                        costs[old_index - 1][new_index] + gap_cost,
                        "delete",
                    ),
                    (
                        costs[old_index][new_index - 1] + gap_cost,
                        "insert",
                    ),
                )
                costs[old_index][new_index], moves[old_index][new_index] = min(
                    options, key=lambda item: item[0]
                )
        aligned: list[tuple[int | None, int | None]] = []
        old_index, new_index = len(old_block), len(new_block)
        while old_index or new_index:
            move = moves[old_index][new_index]
            if move == "replace":
                old_index -= 1
                new_index -= 1
                aligned.append(
                    (old_start + old_index, new_start + new_index)
                )
            elif move == "delete":
                old_index -= 1
                aligned.append((old_start + old_index, None))
            else:
                new_index -= 1
                aligned.append((None, new_start + new_index))
        aligned.reverse()
        return aligned

    matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(old_end - old_start):
                append_row("context", old_start + offset, new_start + offset)
        elif tag == "delete":
            for old_index in range(old_start, old_end):
                append_row("delete", old_index, None)
        elif tag == "insert":
            for new_index in range(new_start, new_end):
                append_row("insert", None, new_index)
        else:
            for old_index, new_index in align_replacement(
                old_start, old_end, new_start, new_end
            ):
                kind = (
                    "replace"
                    if old_index is not None and new_index is not None
                    else "delete"
                    if old_index is not None
                    else "insert"
                )
                append_row(kind, old_index, new_index)

    return {
        "file_change_id": file_change.id,
        "path": file_change.path,
        "old_path": old_path,
        "change_type": file_change.change_type,
        "binary": False,
        "old_line_count": len(old_lines),
        "new_line_count": len(new_lines),
        "rows": rows,
    }


def collect_diff(repo: Path, base_ref: str) -> str:
    tracked = str(
        _git(
            repo,
            "-c",
            "core.quotePath=false",
            "diff",
            "--find-renames",
            "--binary",
            "--no-ext-diff",
            "--no-color",
            "--unified=3",
            base_ref,
            "--",
        )
    )
    additions: list[str] = []
    untracked = _decode_z(
        bytes(
            _git(
                repo,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                text=False,
            )
        )
    )
    for relative in untracked:
        candidate = _safe_file(repo, relative)
        if not candidate:
            continue
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.quotePath=false",
                "diff",
                "--no-index",
                "--binary",
                "--no-color",
                "--unified=3",
                "--",
                "/dev/null",
                relative,
            ],
            cwd=repo,
            capture_output=True,
        )
        if result.returncode in {0, 1}:
            additions.append(
                result.stdout.decode("utf-8", errors="surrogateescape")
            )
    return tracked + "".join(additions)


def _hunk_summary(path: str, added: int, deleted: int, label: str) -> str:
    action = (
        "新增实现"
        if added and not deleted
        else "移除实现"
        if deleted and not added
        else "调整行为"
    )
    scope = label.strip() or Path(path).name
    return f"{action}：{scope}（+{added}/-{deleted}）"[:120]


def parse_patch_hunks(
    diff_text: str,
    file_changes: list[FileChange] | None = None,
) -> list[PatchHunk]:
    """Parse hunks by diff-block order; paths come from NUL-safe name-status."""
    hunks: list[PatchHunk] = []
    blocks = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    changes = file_changes or []
    for change_index, block in enumerate(
        item for item in blocks if item.startswith("diff --git ")
    ):
        change = changes[change_index] if change_index < len(changes) else None
        if (
            change is None
            or change.binary
            or (
                change.change_type == "renamed"
                and change.old_content_sha256 == change.new_content_sha256
            )
        ):
            continue
        lines = block.splitlines()
        header_positions = [
            i for i, line in enumerate(lines) if HUNK_HEADER.match(line)
        ]
        for position_index, start in enumerate(header_positions):
            end = (
                header_positions[position_index + 1]
                if position_index + 1 < len(header_positions)
                else len(lines)
            )
            header = lines[start]
            match = HUNK_HEADER.match(header)
            if not match:
                continue
            body = lines[start + 1 : end]
            added = sum(
                line.startswith("+") and not line.startswith("+++") for line in body
            )
            deleted = sum(
                line.startswith("-") and not line.startswith("---") for line in body
            )
            content = "\n".join([header, *body])
            label = match.group("label")
            hunks.append(
                PatchHunk(
                    id=_id(
                        "hunk",
                        change.old_path or change.path,
                        change.path,
                        header,
                        content,
                    ),
                    file_change_id=change.id,
                    path=change.path,
                    old_path=change.old_path,
                    header=header,
                    old_start=int(match.group("old")),
                    old_count=int(match.group("old_count") or "1"),
                    new_start=int(match.group("new")),
                    new_count=int(match.group("new_count") or "1"),
                    added_lines=added,
                    deleted_lines=deleted,
                    diff=content,
                    summary=_hunk_summary(change.path, added, deleted, label),
                    provenance=Provenance(kind="derived", source="git"),
                )
            )
    return hunks


def _evidence_excerpt(repo: Path, base_ref: str, hunk: PatchHunk) -> str:
    current = _safe_file(repo, hunk.path)
    if current and hunk.new_count:
        content = current.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(hunk.new_start - 1, 0)
        return "\n".join(content[start : start + hunk.new_count])
    old_path = hunk.old_path or hunk.path
    old = _blob_at(repo, base_ref, old_path)
    if old is None:
        return ""
    content = old.decode("utf-8", errors="replace").splitlines()
    start = max(hunk.old_start - 1, 0)
    return "\n".join(content[start : start + hunk.old_count])


def sync_hunk_requirement_ids(proof: ChangeProof) -> None:
    """Compatibility fields are always derived from Mapping records."""
    by_hunk: dict[str, list[str]] = {item.id: [] for item in proof.patch_hunks}
    by_file: dict[str, list[str]] = {item.id: [] for item in proof.file_changes}
    latest_mapping_decisions = {
        item.target_id: item
        for item in proof.review_decisions
        if item.target_type == "mapping"
    }
    for mapping in proof.mappings:
        decision = latest_mapping_decisions.get(mapping.id)
        if decision is not None and decision.decision == "revoked":
            continue
        if (
            mapping.relation == "implemented_by"
            and mapping.to_id in by_hunk
            and mapping.from_id not in by_hunk[mapping.to_id]
        ):
            by_hunk[mapping.to_id].append(mapping.from_id)
        if (
            mapping.relation == "implemented_by_file"
            and mapping.to_id in by_file
            and mapping.from_id not in by_file[mapping.to_id]
        ):
            by_file[mapping.to_id].append(mapping.from_id)
    for hunk in proof.patch_hunks:
        hunk.requirement_ids = by_hunk[hunk.id]
    for file_change in proof.file_changes:
        file_change.requirement_ids = by_file[file_change.id]


def _build_risks(
    requirements: list[Requirement],
    hunks: list[PatchHunk],
    claims: list[Claim],
    file_changes: list[FileChange],
) -> list[ResidualRisk]:
    risks = [
        ResidualRisk(
            id=_id("risk", "verification_not_run", item.id),
            code="verification_not_run",
            severity="medium",
            statement=f"尚未验证 Requirement：{item.text}",
            rationale="测试必须显式关联 Requirement 才能提供覆盖。",
            related_ids=[item.id],
            provenance=Provenance(kind="derived", source="evidence_gate"),
        )
        for item in requirements
    ]
    risks.extend(
        ResidualRisk(
            id=_id("risk", "verification_not_run", item.id),
            code="verification_not_run",
            severity="medium",
            statement=f"尚未验证 Hunk：{item.path} {item.header}",
            rationale="测试必须显式关联 Patch Hunk 才能提供覆盖。",
            related_ids=[item.id],
            provenance=Provenance(kind="derived", source="evidence_gate"),
        )
        for item in hunks
    )
    risks.extend(
        ResidualRisk(
            id=_id("risk", "verification_not_run", item.id),
            code="verification_not_run",
            severity="medium",
            statement=f"尚未验证 FileChange：{item.summary}",
            rationale=(
                "文件级变更需要显式验证，或由其全部 Hunk 的验证覆盖。"
            ),
            related_ids=[item.id],
            provenance=Provenance(kind="derived", source="evidence_gate"),
        )
        for item in file_changes
    )
    inferred = [claim.id for claim in claims if claim.provenance.kind == "inferred"]
    if inferred:
        risks.append(
            ResidualRisk(
                id=_id("risk", "inferred_claims"),
                code="inferred_claims",
                severity="low",
                statement="修改原因由变更内容事后推断，需 Reviewer 确认。",
                rationale="当前分析未导入 Coding Agent 的真实开发轨迹。",
                related_ids=inferred,
                provenance=Provenance(kind="derived", source="evidence_gate"),
            )
        )
    if not file_changes:
        risks.append(
            ResidualRisk(
                id=_id("risk", "empty_change"),
                code="empty_change",
                severity="high",
                statement="所选 Base Ref 与当前工作区之间没有可审核的变化。",
                rationale="无法建立 Requirement 与代码变更的证据链。",
                provenance=Provenance(kind="derived", source="evidence_gate"),
            )
        )
    return risks


def build_change_proof(
    repo: Path,
    *,
    base_ref: str | None = None,
    requirement_texts: list[str] | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> ChangeProof:
    def progress(**payload: object) -> None:
        if progress_callback is not None:
            progress_callback(payload)

    repo = repo.resolve()
    progress(phase="scanning", message="正在读取 Git 变更…")
    base = resolve_base_ref(repo, base_ref)
    base_sha = str(_git(repo, "rev-parse", f"{base}^{{commit}}")).strip()
    head_sha = str(_git(repo, "rev-parse", "HEAD")).strip()
    branch = str(_git(repo, "branch", "--show-current")).strip() or "(detached)"
    file_changes = collect_file_changes(repo, base)
    hunks = parse_patch_hunks(collect_diff(repo, base), file_changes)
    progress(
        phase="analyzing",
        message="正在分析代码块与证据…",
        total=len(file_changes),
    )
    raw_requirements = [
        text for text in (requirement_texts or []) if text.strip()
    ]
    if not raw_requirements:
        log_subject = str(
            _git(repo, "log", "-1", "--format=%s", f"{base}..HEAD", check=False)
        ).strip()
        raw_requirements = [
            log_subject or f"审核相对于 {base} 的当前代码变更"
        ]
    requirements = [
        Requirement(
            id=_id("req", " ".join(raw_text.split())),
            text=" ".join(raw_text.split()),
            original_text=raw_text,
            provenance=Provenance(
                kind="captured" if requirement_texts else "derived",
                source="user_input" if requirement_texts else "git",
            ),
        )
        for raw_text in raw_requirements
    ]
    contract_hash = requirement_contract_hash(requirements)
    evidence: list[Evidence] = []
    claims: list[Claim] = []
    mappings: list[Mapping] = []
    file_by_id = {item.id: item for item in file_changes}
    hunk_totals = {item.id: 0 for item in file_changes}
    for item in hunks:
        hunk_totals[item.file_change_id] += 1
    hunk_completed = {item.id: 0 for item in file_changes}
    reported_files: set[str] = set()

    def report_file_ready(file_change: FileChange) -> None:
        if file_change.id in reported_files:
            return
        reported_files.add(file_change.id)
        progress(
            phase="analyzing",
            message="正在分析代码块与证据…",
            total=len(file_changes),
            loaded=len(reported_files),
            file={
                "id": file_change.id,
                "path": file_change.path,
                "old_path": file_change.old_path,
                "change_type": file_change.change_type,
                "summary": file_change.summary,
            },
        )

    for file_change in file_changes:
        if hunk_totals[file_change.id] == 0:
            report_file_ready(file_change)
    for hunk in hunks:
        excerpt = _evidence_excerpt(repo, base, hunk)
        evidence_record = Evidence(
            id=_id("ev", hunk.id),
            path=hunk.path,
            line=hunk.new_start or hunk.old_start or None,
            statement="该代码片段是当前 Patch Hunk 的可定位仓库证据。",
            content_sha256=_sha256(excerpt.encode("utf-8")),
            content_excerpt=excerpt,
            provenance=Provenance(kind="derived", source="git"),
        )
        claim = Claim(
            id=_id("claim", hunk.id),
            statement=hunk.summary,
            evidence_ids=[evidence_record.id],
            provenance=Provenance(
                kind="inferred", source="post_hoc", confidence=0.55
            ),
        )
        hunk.claim_ids = [claim.id]
        evidence.append(evidence_record)
        claims.append(claim)
        # A single user requirement is a useful post-hoc suggestion, never
        # reviewer-confirmed coverage. Multiple requirements remain unmapped.
        if len(requirements) == 1:
            requirement = requirements[0]
            mappings.append(
                Mapping(
                    id=_id("map", requirement.id, hunk.id),
                    from_id=requirement.id,
                    to_id=hunk.id,
                    relation="implemented_by",
                    explanation="Post-hoc 建议映射，需 Reviewer 明确确认。",
                    confirmed=False,
                    provenance=Provenance(
                        kind="inferred", source="post_hoc", confidence=0.4
                    ),
                )
            )
        hunk_completed[hunk.file_change_id] += 1
        if hunk_completed[hunk.file_change_id] == hunk_totals[hunk.file_change_id]:
            report_file_ready(file_by_id[hunk.file_change_id])
    progress(
        phase="finalizing",
        message="正在整理证据链与 Git 快照…",
        total=len(file_changes),
        loaded=len(reported_files),
    )
    paths = {item.path for item in file_changes}
    file_hashes = {
        path: _sha256(content)
        for path in paths
        if (content := _worktree_content(repo, path)) is not None
    }
    content_tree_hash, patch_fingerprint = canonical_change_identity(repo, base)
    workspace_tree_sha = content_tree_hash
    snapshot_id = git_snapshot_identity(repo, base_sha, content_tree_hash)
    revision_id = _id(
        "revision", snapshot_id, patch_fingerprint, contract_hash, "2"
    )
    snapshot = GitSnapshot(
        base_ref=base,
        base_sha=base_sha,
        head_sha=head_sha,
        workspace_tree_sha=workspace_tree_sha,
        git_snapshot_id=snapshot_id,
        content_tree_hash=content_tree_hash,
        patch_fingerprint=patch_fingerprint,
        revision_id=revision_id,
        file_hashes=file_hashes,
        is_dirty=bool(str(_git(repo, "status", "--porcelain"))),
    )
    proof = ChangeProof(
        change_id=_id(
            "change",
            patch_fingerprint,
            contract_hash,
            "2",
        ),
        revision_id=revision_id,
        review_family_id=_id("family", str(repo), base, base_sha),
        review_series_id=_id(
            "series", str(repo), base, base_sha, contract_hash
        ),
        requirement_contract_hash=contract_hash,
        title=requirements[0].text,
        repository=RepositorySnapshot(
            root=str(repo), name=repo.name, branch=branch
        ),
        git_snapshot=snapshot,
        requirements=requirements,
        requirement_revisions=[
            RequirementRevision(
                requirement_id=item.id,
                revision=item.revision,
                text=item.text,
                original_text=item.original_text,
                category=item.category,
                actor="capture",
            )
            for item in requirements
        ],
        git_snapshot_revisions=[
            GitSnapshotRevision(
                revision_id=revision_id,
                git_snapshot_id=snapshot_id,
                content_tree_hash=content_tree_hash,
                patch_fingerprint=patch_fingerprint,
                head_sha=head_sha,
                branch=branch,
                dirty=snapshot.is_dirty,
                transition="initial",
            )
        ],
        claims=claims,
        evidence=evidence,
        file_changes=file_changes,
        patch_hunks=hunks,
        mappings=mappings,
        risks=_build_risks(requirements, hunks, claims, file_changes),
        assurance=Assurance(
            level="low" if file_changes else "unrated",
            reasons=["尚未完成 Reviewer 确认和显式验证覆盖。"]
            if file_changes
            else ["未发现可分析的变更。"],
        ),
        gate=EvidenceGate(
            status="warning" if file_changes else "blocked",
            reasons=(
                ["unconfirmed_requirement_hunk_mappings", "verification_not_run"]
                if hunks
                else ["file_changes_without_text_hunks"]
                if file_changes
                else ["no_changes"]
            ),
            coverage=GateCoverage(
                requirement_hunk=0.0,
                claim_evidence=1.0 if claims else 0.0,
                verification=0.0,
            ),
        ),
    )
    sync_hunk_requirement_ids(proof)
    return proof


def refresh_evidence_stale(repo: Path, proof: ChangeProof) -> bool:
    """Rebuild deterministic evidence and mark individual records stale."""
    current_tree, current_patch = canonical_change_identity(
        repo, proof.git_snapshot.base_sha
    )
    overall_stale = current_tree != (
        proof.git_snapshot.content_tree_hash
        or proof.git_snapshot.workspace_tree_sha
    )
    fresh = build_change_proof(
        repo,
        base_ref=proof.git_snapshot.base_ref,
        requirement_texts=[item.original_text for item in proof.requirements],
    )
    fresh_hashes = {item.id: item.content_sha256 for item in fresh.evidence}
    for item in proof.evidence:
        item.stale = fresh_hashes.get(item.id) != item.content_sha256
    return overall_stale
