import ast
import fnmatch
import json
import logging
import subprocess
import time
from pathlib import Path

from app.services.agent_runtime import (
    CodingTask,
    RegisteredTool,
    ToolCapability,
    ToolRegistry,
    normalize_task,
)
from app.services.openai.policy import RuntimePolicy, get_runtime_policy
from app.services.retrieval import HybridCodeRetriever
from app.services.sandbox.git_ops import apply_patch as git_apply_patch
from app.services.sandbox.git_ops import diff as git_diff
from app.services.sandbox.git_ops import resolve_repo_path
from app.services.sandbox.git_ops import replace_in_tracked_file, reverse_patch, write_tracked_file
from app.services.sandbox.limits import enforce_patch_limits
from app.services.sandbox.limits import is_path_allowed
from app.services.sandbox.repo_config import RepoConfig
from app.services.sandbox.repo_config import validate_command
from app.services.sandbox.runner import SandboxRunner

logger = logging.getLogger(__name__)

MUTATING_TOOLS = {
    "write_file",
    "replace_in_file",
    "replace_in_files",
    "apply_patch",
}
BUILTIN_TOOL_CAPABILITIES = {
    "get_task_context": ToolCapability.context,
    "get_issue_context": ToolCapability.context,
    "get_repo_config": ToolCapability.context,
    "list_files": ToolCapability.read,
    "glob_file_search": ToolCapability.search,
    "search_code": ToolCapability.search,
    "retrieve_code": ToolCapability.search,
    "read_file": ToolCapability.read,
    "find_definition": ToolCapability.search,
    "get_imports": ToolCapability.search,
    "get_functions": ToolCapability.read,
    "git_log": ToolCapability.version_control,
    "git_blame": ToolCapability.version_control,
    "git_diff": ToolCapability.version_control,
    "write_file": ToolCapability.mutate,
    "replace_in_file": ToolCapability.mutate,
    "replace_in_files": ToolCapability.mutate,
    "apply_patch": ToolCapability.mutate,
    "run_tests": ToolCapability.execute,
    "run_test_file": ToolCapability.execute,
    "run_command": ToolCapability.execute,
}


class ToolExecutionError(RuntimeError):
    def __init__(self, tool_name: str, arguments: dict, message: str, diff_text: str) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.arguments = arguments
        self.diff_text = diff_text


def _check_ripgrep_available() -> bool:
    try:
        subprocess.run(["rg", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ---------------------------------------------------------------------------
# AST helpers (used by find_definition, get_imports, get_functions)
# ---------------------------------------------------------------------------

def _parse_ast(file_path: Path) -> ast.Module | None:
    try:
        return ast.parse(file_path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None


def _classify_test_failure(stdout: str, stderr: str) -> str:
    """Classify test failure mode to help agent choose the right fix strategy."""
    combined = (stdout + stderr).lower()
    if "syntaxerror" in combined:
        return "syntax_error"
    if "importerror" in combined or "modulenotfounderror" in combined:
        return "import_error"
    if "nameerror" in combined:
        return "name_error"
    if "typeerror" in combined:
        return "type_error"
    if "attributeerror" in combined:
        return "attribute_error"
    if "assertionerror" in combined:
        return "assertion_failure"
    if "timeout" in combined or "timed out" in combined:
        return "timeout"
    if "recursionerror" in combined:
        return "recursion_error"
    if "keyerror" in combined or "indexerror" in combined:
        return "lookup_error"
    return "unknown"


# Failure-specific guidance for the agent
FAILURE_GUIDANCE = {
    "syntax_error": "You introduced a syntax error. Read the file you modified and check for missing colons, parentheses, or indentation issues.",
    "import_error": "An import is broken. Use get_imports to check dependencies and find_definition to verify the target exists.",
    "name_error": "You referenced a name that doesn't exist. Use find_definition to check if the function/class/variable is defined.",
    "type_error": "A type mismatch occurred. Check function signatures with find_definition and verify argument types.",
    "attribute_error": "You accessed an attribute that doesn't exist. Use find_definition to inspect the object's class and available methods.",
    "assertion_failure": "The implementation logic is wrong. Compare expected vs actual values in the test output and re-read the task requirements.",
    "timeout": "Your code caused an infinite loop or deadlock. Simplify the fix and avoid recursive patterns.",
    "recursion_error": "Your code has infinite recursion. Check for missing base cases or circular calls.",
    "lookup_error": "You accessed a missing key or index. Add bounds checking or default values.",
    "unknown": "An unexpected error occurred. Read the full test output carefully.",
}


class AgentToolbox:
    def __init__(
        self,
        repo_path: Path,
        repo_config: RepoConfig,
        issue_context: dict | CodingTask | None = None,
        sandbox_runner: SandboxRunner | None = None,
        runtime_policy: RuntimePolicy | None = None,
        *,
        task_context: dict | CodingTask | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.repo_config = repo_config
        original_context = task_context or issue_context
        if original_context is None:
            raise ValueError("task_context_required")
        self.task = normalize_task(original_context)
        self.issue_context = (
            issue_context
            if isinstance(issue_context, dict)
            else self.task.prompt_payload()
        )
        self.sandbox_runner = sandbox_runner or SandboxRunner()
        self._rg_available = _check_ripgrep_available()
        self._code_retriever: HybridCodeRetriever | None = None
        self.runtime_policy = runtime_policy or get_runtime_policy()
        self.tool_registry = tool_registry or ToolRegistry()
        self._active_builtin_tools: set[str] | None = None

    # ------------------------------------------------------------------
    # File exploration tools
    # ------------------------------------------------------------------

    def list_files(self, path: str = ".", limit: int = 200) -> dict:
        root = self.repo_path / path
        if not root.exists():
            return {"files": [], "error": f"directory_not_found: {path}"}
        files = [str(item.relative_to(self.repo_path)).replace("\\", "/") for item in root.rglob("*") if item.is_file()]
        return {"files": files[:limit], "total": len(files), "truncated": len(files) > limit}

    def glob_file_search(self, pattern: str, limit: int = 200) -> dict:
        files = [
            str(item.relative_to(self.repo_path)).replace("\\", "/")
            for item in self.repo_path.glob(pattern)
            if item.is_file()
        ]
        return {
            "files": files[:limit],
            "total": len(files),
            "truncated": len(files) > limit,
            "pattern": pattern,
        }

    def search_code(self, query: str, glob: str | None = None, limit: int = 50) -> dict:
        if not self._rg_available:
            return self._search_code_fallback(query, glob, limit)

        command = ["rg", "--line-number", "--hidden", "--glob", glob or "*", query, str(self.repo_path)]
        try:
            process = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            return {"matches": [], "error": "search_timed_out"}
        except FileNotFoundError:
            return self._search_code_fallback(query, glob, limit)

        matches = []
        for line in process.stdout.splitlines()[:limit]:
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            path, line_no, content = parts
            matches.append({
                "path": str(Path(path).relative_to(self.repo_path)).replace("\\", "/"),
                "line": int(line_no),
                "content": content,
            })
        return {"matches": matches, "total": len(matches)}

    def _search_code_fallback(self, query: str, glob: str | None = None, limit: int = 50) -> dict:
        matches = []
        pattern = glob or "*"
        try:
            for file_path in self.repo_path.rglob("*"):
                if not file_path.is_file():
                    continue
                rel = str(file_path.relative_to(self.repo_path)).replace("\\", "/")
                if not fnmatch.fnmatch(file_path.name, pattern):
                    continue
                try:
                    for line_no, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if query in line:
                            matches.append({"path": rel, "line": line_no, "content": line})
                            if len(matches) >= limit:
                                return {"matches": matches, "total": len(matches), "note": "ripgrep_not_available_used_python_fallback"}
                except (OSError, UnicodeDecodeError):
                    continue
        except Exception:
            pass
        return {"matches": matches, "total": len(matches), "note": "ripgrep_not_available_used_python_fallback"}

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> dict:
        file_path = resolve_repo_path(self.repo_path, path)
        lines = file_path.read_text(encoding="utf-8").splitlines()
        total_lines = len(lines)
        start = (start_line - 1) if start_line else 0
        end = end_line if end_line else total_lines
        start = max(0, min(start, total_lines))
        end = max(start, min(end, total_lines))
        numbered_lines = []
        for i in range(start, end):
            numbered_lines.append(f"{i + 1:4d} | {lines[i]}")
        content = "\n".join(numbered_lines)
        result: dict = {
            "path": path,
            "total_lines": total_lines,
            "showing_lines": f"{start + 1}-{end}",
            "content": content,
        }
        if start > 0:
            result["note"] = f"Showing lines {start + 1}-{end} of {total_lines}. Use start_line/end_line to see other sections."
        if end < total_lines:
            result["remaining_lines"] = total_lines - end
        return result

    def retrieve_code(self, query: str, limit: int = 8) -> dict:
        """Rank relevant code using lexical, symbol, and dependency signals."""
        if not self.runtime_policy.enable_hybrid_retrieval:
            return {"error": "hybrid_retrieval_disabled_by_runtime_policy"}
        if self._code_retriever is None:
            self._code_retriever = HybridCodeRetriever(self.repo_path)
        return self._code_retriever.search(query, limit=limit)

    # ------------------------------------------------------------------
    # Code understanding tools (NEW)
    # ------------------------------------------------------------------

    def find_definition(self, name: str) -> dict:
        """Find where a function, class, or method is defined using Python AST.

        This provides semantic code navigation — the agent can jump directly
        to a definition instead of doing text search. Works for functions,
        async functions, classes, and top-level assignments.
        """
        results = []
        for py_file in sorted(self.repo_path.rglob("*.py")):
            if py_file.name.startswith("."):
                continue
            tree = _parse_ast(py_file)
            if tree is None:
                continue
            rel_path = str(py_file.relative_to(self.repo_path)).replace("\\", "/")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == name:
                        # Extract signature for functions
                        sig = ""
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            args = []
                            for arg in node.args.args:
                                args.append(arg.arg)
                            sig = f"({', '.join(args)})"
                        results.append({
                            "path": rel_path,
                            "line": node.lineno,
                            "type": "function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "class",
                            "signature": sig,
                            "docstring": ast.get_docstring(node) or "",
                        })
                # Top-level assignments (constants, config values)
                if isinstance(node, ast.Assign) and not isinstance(getattr(node, '_parent', None), ast.Module):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            results.append({
                                "path": rel_path,
                                "line": node.lineno,
                                "type": "variable",
                                "signature": "",
                                "docstring": "",
                            })
        return {"definitions": results, "total": len(results)}

    def get_imports(self, path: str) -> dict:
        """Show what a file imports AND what other files import it.

        This gives the agent dependency awareness — when modifying a function,
        it can check which files depend on it and assess impact.
        """
        file_path = resolve_repo_path(self.repo_path, path)
        tree = _parse_ast(file_path)
        if tree is None:
            return {"imports": [], "imported_by": [], "error": "parse_failed"}

        # Forward: what does this file import?
        imports_from = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports_from.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [a.name for a in node.names]
                imports_from.append(f"from {module} import {', '.join(names)}")

        # Reverse: who imports this file?
        # Build module path (e.g., "app/display.py" -> "app.display")
        module_path = path.replace("\\", "/").removesuffix(".py").replace("/", ".")
        imported_by = []
        for py_file in sorted(self.repo_path.rglob("*.py")):
            if py_file == file_path or py_file.name.startswith("."):
                continue
            rel = str(py_file.relative_to(self.repo_path)).replace("\\", "/")
            try:
                content = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # Check various import patterns
            if f"from {module_path} " in content or f"import {module_path}" in content:
                imported_by.append(rel)
            # Also check partial module paths (e.g., "from app.display import X")
            elif module_path in content and ("import" in content):
                # More precise check
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("from ", "import ")):
                        if module_path in stripped:
                            imported_by.append(rel)
                            break

        return {
            "imports": imports_from,
            "imported_by": imported_by,
            "file": path,
        }

    def get_functions(self, path: str) -> dict:
        """List all functions and classes in a file with their line numbers.

        Quick overview of file structure without reading the entire file.
        """
        file_path = resolve_repo_path(self.repo_path, path)
        tree = _parse_ast(file_path)
        if tree is None:
            return {"items": [], "error": "parse_failed"}

        items = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                items.append({
                    "name": node.name,
                    "type": "function",
                    "line": node.lineno,
                    "signature": f"({', '.join(args)})",
                    "docstring": (ast.get_docstring(node) or "")[:100],
                })
            elif isinstance(node, ast.ClassDef):
                methods = []
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(child.name)
                items.append({
                    "name": node.name,
                    "type": "class",
                    "line": node.lineno,
                    "methods": methods,
                    "docstring": (ast.get_docstring(node) or "")[:100],
                })
        return {"items": items, "file": path}

    # ------------------------------------------------------------------
    # Git history tools (NEW)
    # ------------------------------------------------------------------

    def git_log(self, path: str | None = None, limit: int = 10) -> dict:
        """Show recent commits, optionally filtered to a specific file.

        Helps agent understand recent changes that might have introduced bugs.
        """
        command = ["git", "log", f"-{limit}", "--oneline", "--format=%H|%h|%an|%ar|%s"]
        if path:
            command.extend(["--", path])
        try:
            result = subprocess.run(command, cwd=self.repo_path, capture_output=True, text=True, check=True, timeout=10)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return {"commits": [], "error": str(exc)}

        commits = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 4)
            if len(parts) == 5:
                commits.append({
                    "hash": parts[0][:12],
                    "short_hash": parts[1],
                    "author": parts[2],
                    "date": parts[3],
                    "message": parts[4],
                })
        return {"commits": commits, "file": path}

    def git_blame(self, path: str, start_line: int | None = None, end_line: int | None = None) -> dict:
        """Show who last modified each line of a file.

        Helps agent understand the history of specific code sections.
        """
        command = ["git", "blame", "--porcelain"]
        if start_line and end_line:
            command.extend(["-L", f"{start_line},{end_line}"])
        elif start_line:
            command.extend(["-L", f"{start_line},{start_line}"])
        command.append(path)
        try:
            result = subprocess.run(command, cwd=self.repo_path, capture_output=True, text=True, check=True, timeout=10)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return {"blame": [], "error": str(exc)}

        # Parse porcelain format
        commits_info = {}
        lines_blame = []
        current_hash = None
        for line in result.stdout.splitlines():
            if line.startswith("author "):
                if current_hash:
                    commits_info[current_hash] = commits_info.get(current_hash, {})
                    commits_info[current_hash]["author"] = line[7:]
            elif line.startswith("author-time "):
                if current_hash:
                    commits_info[current_hash]["timestamp"] = int(line[12:])
            elif line.startswith("summary "):
                if current_hash:
                    commits_info[current_hash]["message"] = line[8:]
            elif len(line) >= 40 and line[40:41] == " ":
                # This is a blame line: <hash> <orig_line> <final_line> [<count>]
                parts = line.split()
                if parts:
                    current_hash = parts[0]
                    if len(parts) >= 3:
                        try:
                            orig_line = int(parts[1])
                            lines_blame.append({
                                "line": int(parts[2]),
                                "hash": current_hash[:12],
                                "orig_line": orig_line,
                            })
                        except ValueError:
                            pass

        # Enrich with commit info
        for entry in lines_blame:
            full_hash = entry["hash"]
            for h, info in commits_info.items():
                if h.startswith(full_hash):
                    entry["author"] = info.get("author", "")
                    entry["message"] = info.get("message", "")
                    break

        return {"blame": lines_blame[:100], "file": path}  # Cap at 100 lines

    # ------------------------------------------------------------------
    # File editing tools
    # ------------------------------------------------------------------

    def _validate_edit_path(self, path: str) -> None:
        normalized = path.replace("\\", "/")
        task_constraints = self.task.constraints
        allowed_paths = (
            task_constraints.allowed_paths
            or self.repo_config.allowed_paths
        )
        blocked_paths = list(
            dict.fromkeys(
                [
                    *self.repo_config.blocked_paths,
                    *task_constraints.blocked_paths,
                ]
            )
        )
        if not is_path_allowed(
            normalized,
            allowed_paths,
            blocked_paths,
        ):
            raise ValueError(f"path_not_allowed:{normalized}")

    def _diff_result(self) -> dict:
        diff_text = git_diff(self.repo_path)
        task_constraints = self.task.constraints
        max_changed_files = self.repo_config.max_changed_files
        if task_constraints.max_changed_files is not None:
            max_changed_files = min(
                max_changed_files,
                task_constraints.max_changed_files,
            )
        max_diff_lines = self.repo_config.max_diff_lines
        if task_constraints.max_diff_lines is not None:
            max_diff_lines = min(
                max_diff_lines,
                task_constraints.max_diff_lines,
            )
        stats = enforce_patch_limits(
            diff_text,
            (
                task_constraints.allowed_paths
                or self.repo_config.allowed_paths
            ),
            [
                *self.repo_config.blocked_paths,
                *task_constraints.blocked_paths,
            ],
            max_changed_files,
            max_diff_lines,
        )
        return {
            "files_changed_count": stats.files_changed_count,
            "diff_line_count": stats.diff_line_count,
            "diff": diff_text,
        }

    def _validate_python_content(
        self,
        path: str,
        content: str,
        *,
        original_content: str | None = None,
    ) -> None:
        if not path.endswith(".py"):
            return
        if (
            "\\n" in content
            and "\n" not in content
            and original_content is not None
            and "\n" in original_content
        ):
            raise ValueError(
                f"escaped_newlines_detected:{path}: use real line breaks instead of literal \\\\n"
            )
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            location = f"line {exc.lineno}, column {exc.offset}"
            raise ValueError(
                f"python_syntax_invalid:{path}:{location}: {exc.msg}"
            ) from exc

    def _validate_changed_python_files(self, diff_text: str) -> None:
        changed_paths = {
            line.removeprefix("+++ b/")
            for line in diff_text.splitlines()
            if line.startswith("+++ b/") and line.endswith(".py")
        }
        for path in sorted(changed_paths):
            file_path = resolve_repo_path(self.repo_path, path)
            if file_path.exists():
                self._validate_python_content(
                    path,
                    file_path.read_text(encoding="utf-8"),
                )

    def write_file(self, path: str, content: str) -> dict:
        self._validate_edit_path(path)
        original_content = resolve_repo_path(self.repo_path, path).read_text(encoding="utf-8")
        try:
            self._validate_python_content(
                path,
                content,
                original_content=original_content,
            )
            write_tracked_file(self.repo_path, path, content)
            return self._diff_result()
        except Exception:
            write_tracked_file(self.repo_path, path, original_content)
            raise

    def replace_in_file(self, path: str, old_text: str, new_text: str) -> dict:
        self._validate_edit_path(path)
        original_content = resolve_repo_path(self.repo_path, path).read_text(encoding="utf-8")
        try:
            replace_in_tracked_file(self.repo_path, path, old_text, new_text)
            updated_content = resolve_repo_path(self.repo_path, path).read_text(encoding="utf-8")
            self._validate_python_content(
                path,
                updated_content,
                original_content=original_content,
            )
            return self._diff_result()
        except Exception:
            write_tracked_file(self.repo_path, path, original_content)
            raise

    def replace_in_files(self, operations: list[dict]) -> dict:
        """Apply a small set of exact replacements as one workspace transaction."""
        originals: dict[str, str] = {}
        try:
            for operation in operations:
                path = str(operation["path"])
                self._validate_edit_path(path)
                if path not in originals:
                    originals[path] = resolve_repo_path(
                        self.repo_path,
                        path,
                    ).read_text(encoding="utf-8")
                replace_in_tracked_file(
                    self.repo_path,
                    path,
                    str(operation["old_text"]),
                    str(operation.get("new_text") or ""),
                )
                updated = resolve_repo_path(
                    self.repo_path,
                    path,
                ).read_text(encoding="utf-8")
                self._validate_python_content(
                    path,
                    updated,
                    original_content=originals[path],
                )
            return self._diff_result()
        except Exception:
            for path, content in originals.items():
                write_tracked_file(self.repo_path, path, content)
            raise

    def apply_patch(self, unified_diff: str) -> dict:
        git_apply_patch(self.repo_path, unified_diff)
        try:
            result = self._diff_result()
            self._validate_changed_python_files(result["diff"])
            return result
        except Exception:
            reverse_patch(self.repo_path, unified_diff)
            raise

    def git_diff(self) -> dict:
        return {"diff": git_diff(self.repo_path)}

    # ------------------------------------------------------------------
    # Test execution tools
    # ------------------------------------------------------------------

    def run_tests(self, runner: str | None = None) -> dict:
        """Run the full test suite. Returns failure classification to guide retry."""
        start = time.perf_counter()
        result = self.sandbox_runner.run_tests(self.repo_path, self.repo_config.test_command)
        duration_ms = int((time.perf_counter() - start) * 1000)

        response = {
            "exit_code": result.exit_code,
            "stdout": result.stdout[-3000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
            "duration_ms": duration_ms,
        }

        # Classify failure to help agent choose retry strategy
        if result.exit_code != 0:
            failure_type = _classify_test_failure(result.stdout, result.stderr)
            response["failure_type"] = failure_type
            response["guidance"] = FAILURE_GUIDANCE.get(failure_type, FAILURE_GUIDANCE["unknown"])

        return response

    def run_test_file(self, test_path: str, test_name: str | None = None) -> dict:
        """Run a specific test file or test function for targeted verification.

        Much faster than running the full suite when you only need to check
        a specific test case.
        """
        command = f"{self.repo_config.test_command} {test_path}"
        if test_name:
            command += f"::{test_name}"
        start = time.perf_counter()
        result = self.sandbox_runner.run_tests(self.repo_path, command)
        duration_ms = int((time.perf_counter() - start) * 1000)

        response = {
            "exit_code": result.exit_code,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-1500:] if result.stderr else "",
            "duration_ms": duration_ms,
            "test_path": test_path,
            "test_name": test_name,
        }

        if result.exit_code != 0:
            failure_type = _classify_test_failure(result.stdout, result.stderr)
            response["failure_type"] = failure_type
            response["guidance"] = FAILURE_GUIDANCE.get(failure_type, FAILURE_GUIDANCE["unknown"])

        return response

    def run_command(self, command: str) -> dict:
        """Run a repository command in the isolated sandbox."""
        validate_command(command)
        start = time.perf_counter()
        if hasattr(self.sandbox_runner, "run"):
            result = self.sandbox_runner.run(
                self.repo_path,
                command,
                create_venv=False,
                allow_network=False,
            )
        else:
            result = self.sandbox_runner.run_tests(
                self.repo_path,
                command,
            )
        return {
            "command": command,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-4000:] if result.stdout else "",
            "stderr": result.stderr[-3000:] if result.stderr else "",
            "duration_ms": int(
                (time.perf_counter() - start) * 1000
            ),
        }

    # ------------------------------------------------------------------
    # Context tools
    # ------------------------------------------------------------------

    def get_task_context(self) -> dict:
        return self.task.prompt_payload()

    def get_issue_context(self) -> dict:
        """Compatibility alias for legacy GitHub and SWE adapters."""
        return self.issue_context

    def register_tool(
        self,
        *,
        name: str,
        schema: dict,
        handler,
        capability: ToolCapability = ToolCapability.read,
        source: str = "extension",
    ) -> None:
        self.tool_registry.register(
            RegisteredTool(
                name=name,
                schema=schema,
                handler=handler,
                capability=capability,
                source=source,
            )
        )

    def set_active_tools(self, names: list[str] | None) -> None:
        self._active_builtin_tools = (
            None if names is None else set(names)
        )
        self.tool_registry.set_active(names)

    def get_active_tools(self) -> list[str]:
        builtin = [
            schema["function"]["name"]
            for schema in self.tool_schemas(include_extensions=False)
        ]
        return [*builtin, *self.tool_registry.names()]

    def get_repo_config(self) -> dict:
        return {
            "language": self.repo_config.language,
            "test_command": self.repo_config.test_command,
            "install_command": self.repo_config.install_command,
            "allowed_paths": self.repo_config.allowed_paths,
            "blocked_paths": self.repo_config.blocked_paths,
            "max_changed_files": self.repo_config.max_changed_files,
            "max_diff_lines": self.repo_config.max_diff_lines,
        }

    # ------------------------------------------------------------------
    # Tool schemas
    # ------------------------------------------------------------------

    def tool_schemas(
        self,
        *,
        include_extensions: bool = True,
    ) -> list[dict]:
        schemas = [
            # --- File exploration ---
            {"type": "function", "function": {"name": "list_files", "description": "List repository files. Returns file paths, total count, and whether results were truncated.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory path relative to repo root (default: '.')"}, "limit": {"type": "integer", "description": "Max files to return (default 200)"}}, "required": []}}},
            {"type": "function", "function": {"name": "glob_file_search", "description": "Find repository files by a glob pattern such as '**/parser*.py'.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "limit": {"type": "integer", "description": "Max files to return (default 200)"}}, "required": ["pattern"]}}},
            {"type": "function", "function": {"name": "search_code", "description": "Search code using ripgrep (with Python fallback). Returns matches with file path, line number, and content.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search pattern (regex supported)"}, "glob": {"type": "string", "description": "File glob filter, e.g. '*.py'"}, "limit": {"type": "integer", "description": "Max matches to return (default 50)"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "read_file", "description": "Read a repository file with line numbers. Returns numbered lines for precise reference. Use start_line/end_line for large files.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "File path relative to repo root"}, "start_line": {"type": "integer", "description": "1-indexed start line (default: 1)"}, "end_line": {"type": "integer", "description": "1-indexed end line (default: end of file)"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "retrieve_code", "description": "Hybrid code retrieval using BM25 lexical ranking, AST symbols, dependency graph expansion, and reciprocal-rank fusion. Returns ranked files, snippets, and per-channel provenance.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Natural-language issue, error, or symbol query"}, "limit": {"type": "integer", "description": "Maximum ranked files (default 8, max 20)"}}, "required": ["query"]}}},
            # --- Code understanding (NEW) ---
            {"type": "function", "function": {"name": "find_definition", "description": "Find where a function, class, or variable is DEFINED using Python AST parsing. Use this instead of search_code when you need to locate a specific symbol. Returns file path, line number, type (function/class/variable), signature, and docstring.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "The function, class, or variable name to find"}}, "required": ["name"]}}},
            {"type": "function", "function": {"name": "get_imports", "description": "Show what a file imports AND what other files import it. Use this to understand dependencies before modifying a function — you can see which files will be affected.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "File path relative to repo root"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "get_functions", "description": "List all functions, classes, and methods in a file with line numbers and signatures. Quick way to understand file structure without reading the whole file.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "File path relative to repo root"}}, "required": ["path"]}}},
            # --- Git history (NEW) ---
            {"type": "function", "function": {"name": "git_log", "description": "Show recent git commits, optionally filtered to a specific file. Helps understand what changes might have introduced a bug. Returns hash, author, date, and message.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Optional: filter to a specific file"}, "limit": {"type": "integer", "description": "Max commits to return (default 10)"}}, "required": []}}},
            {"type": "function", "function": {"name": "git_blame", "description": "Show who last modified each line of a file. Helps understand the history and authorship of specific code sections.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "File path relative to repo root"}, "start_line": {"type": "integer", "description": "Optional: start line number"}, "end_line": {"type": "integer", "description": "Optional: end line number"}}, "required": ["path"]}}},
            # --- File editing ---
            {"type": "function", "function": {"name": "write_file", "description": "Rewrite an existing repository file with full UTF-8 content after reading it first", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
            {"type": "function", "function": {"name": "replace_in_file", "description": "Replace a specific text snippet inside an existing repository file. old_text must match exactly.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string", "description": "Exact text to find (must match uniquely)"}, "new_text": {"type": "string", "description": "Replacement text"}}, "required": ["path", "old_text", "new_text"]}}},
            {"type": "function", "function": {"name": "replace_in_files", "description": "Atomically apply up to five exact text replacements across repository files. All operations roll back if one fails.", "parameters": {"type": "object", "properties": {"operations": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}}, "required": ["operations"]}}},
            {"type": "function", "function": {"name": "apply_patch", "description": "Apply a unified diff patch", "parameters": {"type": "object", "properties": {"unified_diff": {"type": "string"}}, "required": ["unified_diff"]}}},
            {"type": "function", "function": {"name": "git_diff", "description": "Get current git diff of all changes", "parameters": {"type": "object", "properties": {}, "required": []}}},
            # --- Test execution ---
            {"type": "function", "function": {"name": "run_tests", "description": "Run the FULL test suite. Returns exit_code, stdout (truncated), stderr, duration_ms, and on failure: failure_type + guidance for retry strategy.", "parameters": {"type": "object", "properties": {"runner": {"type": "string"}}, "required": []}}},
            {"type": "function", "function": {"name": "run_test_file", "description": "Run a SPECIFIC test file or test function. Much faster than run_tests for targeted verification. Example: run_test_file('tests/test_display.py') or run_test_file('tests/test_display.py', 'test_format_display_name_none').", "parameters": {"type": "object", "properties": {"test_path": {"type": "string", "description": "Path to test file relative to repo root"}, "test_name": {"type": "string", "description": "Optional: specific test function name"}}, "required": ["test_path"]}}},
            {"type": "function", "function": {"name": "run_command", "description": "Run a repository build, lint, type-check, or test command inside the isolated sandbox. Commands must match the configured safety whitelist.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
            # --- Context ---
            {"type": "function", "function": {"name": "get_task_context", "description": "Get the source-independent coding task, including objective, intent, requirements, constraints, and delivery target.", "parameters": {"type": "object", "properties": {}, "required": []}}},
            {"type": "function", "function": {"name": "get_issue_context", "description": "Compatibility alias for legacy GitHub Issue and SWE-bench task context.", "parameters": {"type": "object", "properties": {}, "required": []}}},
            {"type": "function", "function": {"name": "get_repo_config", "description": "Get repository configuration including allowed/blocked paths and limits", "parameters": {"type": "object", "properties": {}, "required": []}}},
        ]
        if not self.runtime_policy.enable_hybrid_retrieval:
            schemas = [
                schema
                for schema in schemas
                if schema["function"]["name"] != "retrieve_code"
            ]
        if self.repo_config.language != "python":
            python_specific = {
                "find_definition",
                "get_imports",
                "get_functions",
                "run_test_file",
            }
            schemas = [
                schema
                for schema in schemas
                if schema["function"]["name"] not in python_specific
            ]
        if not self.task.allows_mutation:
            schemas = [
                schema
                for schema in schemas
                if schema["function"]["name"] not in MUTATING_TOOLS
            ]
        if self._active_builtin_tools is not None:
            schemas = [
                schema
                for schema in schemas
                if schema["function"]["name"]
                in self._active_builtin_tools
            ]
        if include_extensions:
            schemas.extend(self.tool_registry.schemas())
        return schemas

    def dispatch(self, name: str, arguments_json: str) -> dict:
        try:
            args = json.loads(arguments_json or "{}")
        except json.JSONDecodeError as exc:
            logger.warning(
                "agent_tool_arguments_json_failed",
                extra={"tool_name": name, "arguments_json": arguments_json, "error": str(exc)},
            )
            return {
                "error": f"tool_arguments_invalid_json:{name}: {exc}",
                "recoverable": True,
                "tool": name,
                "arguments_json": arguments_json,
                "guidance": "Retry the same tool call with valid JSON arguments matching the tool schema.",
            }
        args = self._normalize_tool_arguments(name, args)
        extension = self.tool_registry.get(name)
        if extension is not None:
            if not self.tool_registry.is_active(name):
                return {
                    "error": f"tool_not_active:{name}",
                    "recoverable": True,
                }
            if (
                extension.capability == ToolCapability.mutate
                and not self.task.allows_mutation
            ):
                return {
                    "error": f"tool_not_allowed_for_task:{name}",
                    "recoverable": False,
                }
            try:
                return extension.handler(**args)
            except Exception as exc:
                return {
                    "error": f"tool_call_failed:{name}: {exc}",
                    "recoverable": True,
                    "tool": name,
                }
        if (
            self._active_builtin_tools is not None
            and name not in self._active_builtin_tools
        ):
            return {
                "error": f"tool_not_active:{name}",
                "recoverable": True,
            }
        if name in MUTATING_TOOLS and not self.task.allows_mutation:
            return {
                "error": f"tool_not_allowed_for_task:{name}",
                "recoverable": False,
            }
        try:
            return getattr(self, name)(**args)
        except Exception as exc:
            logger.warning("agent_tool_call_failed", extra={"tool_name": name, "arguments": args, "error": str(exc)})
            if name in MUTATING_TOOLS:
                raise ToolExecutionError(
                    name,
                    args,
                    f"tool_call_failed:{name}: {exc}",
                    git_diff(self.repo_path),
                ) from exc
            return {
                "error": f"tool_call_failed:{name}: {exc}",
                "recoverable": True,
                "tool": name,
                "arguments": args,
                "guidance": (
                    "The tool call failed but the agent loop is still running. "
                    "Use list_files/search_code to find the correct path or adjust the arguments before retrying."
                ),
            }

    @staticmethod
    def _normalize_tool_arguments(name: str, args: dict) -> dict:
        """Accept common model-produced aliases for tool arguments.

        Real coding-agent rollouts show that models sometimes emit plausible
        argument names that are not exactly in the schema, such as
        ``read_file(file_path=...)`` or ``apply_patch(patch=...)``. Normalizing
        these aliases keeps the harness focused on the code-fixing task instead
        of failing on harmless naming drift.
        """
        normalized = dict(args)
        if name in {"read_file", "get_imports", "get_functions", "git_blame"} and "path" not in normalized:
            if "file_path" in normalized:
                normalized["path"] = normalized.pop("file_path")
            elif "filename" in normalized:
                normalized["path"] = normalized.pop("filename")

        if name == "search_code":
            # Some models pass a path scope even though the tool only supports
            # query/glob. Treat path as an optional glob when it looks file-like;
            # otherwise ignore it rather than failing the rollout.
            path = normalized.pop("path", None)
            if path and "glob" not in normalized:
                path_text = str(path)
                if "*" in path_text or "." in Path(path_text).name:
                    normalized["glob"] = path_text

        if name == "list_files" and "path" not in normalized:
            if "directory" in normalized:
                normalized["path"] = normalized.pop("directory")
            elif "dir" in normalized:
                normalized["path"] = normalized.pop("dir")

        if name == "apply_patch" and "unified_diff" not in normalized:
            if "patch" in normalized:
                normalized["unified_diff"] = normalized.pop("patch")
            elif "diff" in normalized:
                normalized["unified_diff"] = normalized.pop("diff")

        if name == "run_test_file" and "test_path" not in normalized:
            if "path" in normalized:
                normalized["test_path"] = normalized.pop("path")
            elif "file_path" in normalized:
                normalized["test_path"] = normalized.pop("file_path")

        return normalized
