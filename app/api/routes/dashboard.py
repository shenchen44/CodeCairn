from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models.task import Task, TaskArtifactType, TaskStatus
from app.services.task_runner.orchestrator import get_artifact_content
from app.db.session import get_db
from app.services.github.auth import GitHubAuthService
from app.services.github.pulls import GitHubPullRequestService
from app.services.evaluation import build_agent_metrics
from app.services.sandbox.limits import parse_diff_stats
from app.services.task_runner.orchestrator import create_conflict_resolution_task, create_integration_task

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

GENERIC_ROOT_CAUSE_VALUES = {
    "",
    "Issue-specific bug",
    "Issue specific bug",
    "No root cause summary captured.",
}
GENERIC_CHANGE_VALUES = {
    "",
    "Minimal targeted patch",
    "No structured changes captured.",
}


class IntegrationRequest(BaseModel):
    task_ids: list[str]
    guidance: str | None = None


class ConflictResolutionRequest(BaseModel):
    guidance: str | None = None


def _extractget_artifact_content(task: Task, artifact_type: TaskArtifactType, *keys: str) -> str | dict | None:
    """Extract nested content from an artifact, following dot-notation keys."""
    content = get_artifact_content(task, artifact_type)
    if not isinstance(content, dict):
        return content if not keys else None
    result = content
    for key in keys:
        if isinstance(result, dict):
            result = result.get(key)
        else:
            return None
    return result


def _installation_id_for_task(task: Task) -> int | None:
    raw_webhook = get_artifact_content(task, TaskArtifactType.raw_webhook)
    return ((raw_webhook or {}).get("installation") or {}).get("id") if isinstance(raw_webhook, dict) else None


def _resolution_link_for_task(task: Task) -> dict:
    artifact = get_artifact_content(task, TaskArtifactType.resolution_link)
    return artifact if isinstance(artifact, dict) else {}


def _failure_detail_for_task(task: Task) -> str:
    if not task.failure_reason:
        return "Agent task has not opened a pull request yet."
    reason = task.failure_reason.get("reason") or "failed"
    details = task.failure_reason.get("details")
    if isinstance(details, dict):
        error = details.get("error")
        if error:
            return f"{reason}: {error}"
    return reason


def _extract_changes_from_pr_body(pr_body: str) -> list[str]:
    if not pr_body:
        return []
    lines = pr_body.splitlines()
    in_changes = False
    changes: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "## Changes":
            in_changes = True
            continue
        if in_changes and stripped.startswith("## "):
            break
        if in_changes and stripped.startswith("- "):
            changes.append(stripped[2:])
    return changes


def _extract_section_from_pr_body(pr_body: str, heading: str) -> str:
    if not pr_body:
        return ""
    lines = pr_body.splitlines()
    in_section = False
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == heading:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if in_section:
            collected.append(line.rstrip())
    return "\n".join(line for line in collected if line.strip()).strip()


def _is_generic_summary_sentence(value: object) -> bool:
    if not isinstance(value, str):
        return True
    normalized = " ".join(value.strip().split()).lower()
    if not normalized:
        return True
    if normalized in {entry.lower() for entry in GENERIC_ROOT_CAUSE_VALUES | GENERIC_CHANGE_VALUES}:
        return True
    if normalized.startswith("fixes #") and "minimal patch" in normalized:
        return True
    return False


_is_generic_root_cause = _is_generic_summary_sentence  # Alias for semantic clarity


def _normalize_changes(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    changes = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if not changes:
        return []
    if len(changes) == 1 and _is_generic_summary_sentence(changes[0]):
        return []
    return changes


def _summarize_diff_changes(diff_text: str) -> list[str]:
    if not diff_text:
        return []
    stats = parse_diff_stats(diff_text)
    if not stats.changed_files:
        return []
    summaries: list[str] = []
    lowered_diff = diff_text.lower()
    for path in stats.changed_files:
        lowered = path.lower()
        if "/test" in lowered or lowered.startswith("test") or "/tests/" in lowered or lowered.startswith("tests/"):
            summaries.append(f"Updated tests in {path}")
        else:
            summaries.append(f"Updated {path}")
    if " none" in lowered_diff or "none" in lowered_diff:
        summaries.append("Added handling for None-like edge cases")
    if "default=" in lowered_diff:
        summaries.append("Extended the function API with a safe default return path")
    if "zerodivisionerror" in lowered_diff or "b == 0" in lowered_diff:
        summaries.append("Preserved safe division behavior for zero-denominator inputs")
    if stats.diff_line_count:
        summaries.append(f"Kept the patch small at about {stats.diff_line_count} changed diff lines")
    return summaries


def _summarize_diff_root_cause(diff_text: str) -> str:
    if not diff_text:
        return ""
    stats = parse_diff_stats(diff_text)
    non_test_files = [
        path
        for path in stats.changed_files
        if not (
            path.lower().startswith("test")
            or path.lower().startswith("tests/")
            or "/tests/" in path.lower()
            or "/test" in path.lower()
        )
    ]
    if non_test_files:
        if len(non_test_files) == 1:
            return f"Adjusted behavior in {non_test_files[0]} and validated the fix with a focused patch."
        return f"Adjusted behavior across {len(non_test_files)} source files and kept the change targeted."
    if stats.changed_files:
        return "Focused on validating behavior with targeted test updates."
    return ""


def _merge_status_from_payload(pr_payload: dict) -> tuple[str, str, bool]:
    mergeable = pr_payload.get("mergeable")
    mergeable_state = (pr_payload.get("mergeable_state") or "").strip().lower()
    if mergeable is False or mergeable_state in {"dirty", "conflicting"}:
        return ("conflicting", "Conflict", True)
    if mergeable_state == "blocked":
        return ("blocked", "Blocked", False)
    if mergeable_state == "behind":
        return ("behind", "Behind", False)
    if mergeable_state in {"unstable", "draft", "unknown"} or mergeable is None:
        return ("checking", "Checking", False)
    if mergeable is True or mergeable_state in {"clean", "has_hooks"}:
        return ("clean", "Ready", False)
    return ("unknown", "Unknown", False)


@router.get("", response_class=Response)
def dashboard_page() -> Response:
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PR Review Workspace</title>
  <style>
    :root {
      /* Colors - GitHub/Linear inspired neutral palette */
      --bg-canvas: #f6f8fa;
      --bg-surface: #ffffff;
      --bg-header: #ffffff;
      --bg-subtle: #f3f4f6;
      --bg-code: #f6f8fa;
      
      --border-default: #d0d7de;
      --border-muted: #e5e7eb;
      
      --text-main: #24292f;
      --text-muted: #57606a;
      --text-link: #0969da;
      
      --btn-bg: #f6f8fa;
      --btn-border: #d0d7de;
      --btn-hover: #f3f4f6;
      --btn-primary-bg: #1f2328;
      --btn-primary-text: #ffffff;
      --btn-primary-hover: #33383f;
      --btn-success-bg: #1f883d;
      --btn-success-hover: #1a7f37;

      /* Status Colors */
      --status-clean-bg: #dafbe1;
      --status-clean-fg: #1a7f37;
      --status-conflict-bg: #ffebe9;
      --status-conflict-fg: #cf222e;
      --status-warn-bg: #fff8c5;
      --status-warn-fg: #9a6700;
      --status-checking-bg: #ddf4ff;
      --status-checking-fg: #0969da;
      --status-neutral-bg: #eaeef2;
      --status-neutral-fg: #57606a;

      /* Typography */
      --font-ui: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: var(--font-ui);
      background-color: var(--bg-canvas);
      color: var(--text-main);
      font-size: 14px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }

    /* Layout */
    .header {
      background: var(--bg-header);
      border-bottom: 1px solid var(--border-default);
      padding: 16px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 10;
    }

    .header-title {
      font-size: 16px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .header-stats {
      font-size: 13px;
      color: var(--text-muted);
    }

    .container {
      max-width: 1440px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      grid-template-columns: 1fr 340px;
      gap: 24px;
      align-items: start;
    }

    /* Common Components */
    .card {
      background: var(--bg-surface);
      border: 1px solid var(--border-default);
      border-radius: 6px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    }

    button {
      font-family: inherit;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      padding: 5px 12px;
      border-radius: 6px;
      border: 1px solid var(--btn-border);
      background: var(--btn-bg);
      color: var(--text-main);
      transition: all 0.15s ease;
      line-height: 20px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
    }

    button:hover:not(:disabled) {
      background: var(--btn-hover);
    }

    button.primary {
      background: var(--btn-primary-bg);
      color: var(--btn-primary-text);
      border-color: var(--btn-primary-bg);
    }

    button.primary:hover:not(:disabled) {
      background: var(--btn-primary-hover);
      border-color: var(--btn-primary-hover);
    }

    button.success {
      background: var(--btn-success-bg);
      color: var(--btn-primary-text);
      border-color: var(--btn-success-bg);
    }

    button.success:hover:not(:disabled) {
      background: var(--btn-success-hover);
      border-color: var(--btn-success-hover);
    }

    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 500;
      line-height: 18px;
    }

    /* Merge Status Colors */
    .badge.clean { background: var(--status-clean-bg); color: var(--status-clean-fg); }
    .badge.conflicting { background: var(--status-conflict-bg); color: var(--status-conflict-fg); border: 1px solid rgba(207,34,46,0.2); }
    .badge.behind, .badge.blocked { background: var(--status-warn-bg); color: var(--status-warn-fg); }
    .badge.checking { background: var(--status-checking-bg); color: var(--status-checking-fg); }
    .badge.unknown { background: var(--status-neutral-bg); color: var(--status-neutral-fg); }

    /* PR List */
    .pr-list {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .empty-state {
      padding: 48px;
      text-align: center;
      color: var(--text-muted);
      border: 1px dashed var(--border-default);
      border-radius: 6px;
      background: transparent;
    }

    /* PR Item Details */
    .pr-item {
      overflow: hidden;
    }

    .pr-item-header {
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-muted);
      background: var(--bg-canvas);
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
    }

    .pr-item-header-left {
      display: flex;
      gap: 12px;
    }

    .pr-checkbox {
      margin-top: 4px;
      width: 14px;
      height: 14px;
      cursor: pointer;
    }

    .pr-meta {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 4px;
      display: flex;
      gap: 8px;
      align-items: center;
    }

    .pr-repo { font-family: var(--font-mono); color: var(--text-main); font-size: 12px; }
    
    .pr-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--text-main);
      text-decoration: none;
    }

    .pr-title:hover {
      color: var(--text-link);
      text-decoration: underline;
    }

    .pr-body {
      padding: 16px;
    }

    .pr-section {
      margin-bottom: 16px;
    }

    .pr-section:last-child {
      margin-bottom: 0;
    }

    .pr-section-title {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      color: var(--text-muted);
      letter-spacing: 0.05em;
      margin-bottom: 8px;
    }

    .pr-text {
      color: var(--text-main);
    }

    .pr-changes-list {
      padding-left: 18px;
      margin: 0;
      color: var(--text-main);
    }

    .pr-changes-list li {
      margin-bottom: 4px;
    }

    .pr-merge-detail {
      font-size: 13px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .conflict-warning {
      color: var(--status-conflict-fg);
      font-weight: 500;
    }

    /* Diff Accordion */
    details.diff-panel {
      border: 1px solid var(--border-muted);
      border-radius: 6px;
      overflow: hidden;
      margin-top: 16px;
    }

    details.diff-panel summary {
      background: var(--bg-subtle);
      padding: 8px 12px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      user-select: none;
      color: var(--text-muted);
    }
    
    details.diff-panel summary:hover {
      background: var(--border-muted);
      color: var(--text-main);
    }

    .diff-content {
      margin: 0;
      padding: 12px;
      background: var(--bg-code);
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.4;
      overflow-x: auto;
      max-height: 400px;
      color: var(--text-main);
      border-top: 1px solid var(--border-muted);
    }

    .pr-actions {
      padding: 12px 16px;
      background: var(--bg-surface);
      border-top: 1px solid var(--border-muted);
      display: flex;
      gap: 8px;
      justify-content: flex-end;
    }

    /* Sidebar */
    .sidebar {
      position: sticky;
      top: 80px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .sidebar-panel {
      padding: 16px;
    }

    .sidebar-title {
      font-size: 14px;
      font-weight: 600;
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .sidebar-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 12px;
    }

    textarea {
      width: 100%;
      min-height: 100px;
      padding: 8px 12px;
      border: 1px solid var(--border-default);
      border-radius: 6px;
      background: var(--bg-surface);
      font-family: inherit;
      font-size: 13px;
      resize: vertical;
      margin-bottom: 12px;
      color: var(--text-main);
    }

    textarea:focus {
      outline: none;
      border-color: var(--text-link);
      box-shadow: 0 0 0 3px rgba(9, 105, 218, 0.3);
    }

    .selected-list {
      list-style: none;
      margin: 0 0 16px 0;
      padding: 0;
      font-size: 13px;
      max-height: 200px;
      overflow-y: auto;
    }

    .selected-list li {
      padding: 6px 0;
      border-bottom: 1px solid var(--border-muted);
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .selected-list li:last-child {
      border-bottom: none;
    }

    .selected-count-badge {
      background: var(--text-muted);
      color: white;
      padding: 2px 6px;
      border-radius: 10px;
      font-size: 11px;
    }

    @media (max-width: 1024px) {
      .container { grid-template-columns: 1fr; }
      .sidebar { position: static; }
    }
  </style>
</head>
<body>

  <header class="header">
    <div class="header-title">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3v12"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>
      Integration Workspace
    </div>
    <div class="header-stats">
      <span id="openCount">0</span> Agent Tasks
      <button id="refreshBtn" style="margin-left: 12px;">Refresh</button>
    </div>
  </header>

  <div class="container">
    <main class="pr-list" id="prList">
      <div class="empty-state">Loading PRs...</div>
    </main>

    <aside class="sidebar">
      <div class="card sidebar-panel">
        <div class="sidebar-title">
          Selected for Integration
          <span class="selected-count-badge" id="selectedCount">0</span>
        </div>
        
        <ul class="selected-list" id="selectedSummary">
          <li style="color: var(--text-muted); border: none;">No PRs selected.</li>
        </ul>

        <div class="sidebar-title" style="margin-top: 24px;">Integration Strategy</div>
        <p class="sidebar-desc">Provide context to the agent on how these PRs should be combined.</p>
        
        <textarea id="guidance" placeholder="e.g. Combine the DB schema updates first, then apply the UI tweaks. Resolve the dependency conflict by keeping the newer version."></textarea>
        
        <button id="integrateBtn" class="primary" style="width: 100%;" disabled>Create Integration Task</button>
      </div>
    </aside>
  </div>

  <script>
    const state = {
      items: [],
      selected: new Set()
    };

    function escapeHtml(unsafe) {
      return (unsafe || '').toString()
         .replace(/&/g, "&amp;")
         .replace(/</g, "&lt;")
         .replace(/>/g, "&gt;")
         .replace(/"/g, "&quot;")
         .replace(/'/g, "&#039;");
    }

    function updateSidebar() {
      const countEl = document.getElementById('selectedCount');
      const summaryEl = document.getElementById('selectedSummary');
      const btn = document.getElementById('integrateBtn');
      
      countEl.textContent = state.selected.size;
      
      if (state.selected.size === 0) {
        summaryEl.innerHTML = '<li style="color: var(--text-muted); border: none;">No PRs selected.</li>';
        btn.disabled = true;
        btn.textContent = 'Create Integration Task';
      } else {
        const selectedItems = state.items.filter(item => state.selected.has(item.task_id));
        summaryEl.innerHTML = selectedItems.map(item => 
          `<li><span class="pr-repo">#${item.pr_number}</span> <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(item.title)}</span></li>`
        ).join('');
        
        btn.disabled = false;
        btn.textContent = `Integrate ${state.selected.size} PR${state.selected.size > 1 ? 's' : ''}`;
      }
    }

    async function loadPRs() {
      const listEl = document.getElementById('prList');
      listEl.innerHTML = '<div class="empty-state">Loading PRs...</div>';
      
      try {
        const response = await fetch('/dashboard/prs');
        if (!response.ok) throw new Error('Failed to fetch');
        state.items = await response.json();
        
        document.getElementById('openCount').textContent = state.items.length;
        
        if (state.items.length === 0) {
          listEl.innerHTML = '<div class="empty-state card">No agent tasks found.</div>';
          updateSidebar();
          return;
        }

        listEl.innerHTML = state.items.map(item => {
          const isSelected = state.selected.has(item.task_id);
          const isConflict = item.merge_conflict;
          const hasPr = item.pr_number !== null && item.pr_number !== undefined;
          const titleUrl = hasPr ? item.pr_url : item.issue_url;
          const titleNumber = hasPr ? `#${item.pr_number}` : (item.issue_number ? `issue #${item.issue_number}` : item.status);
          
          let conflictWarning = '';
          if (isConflict) {
            conflictWarning = `<div class="pr-merge-detail conflict-warning">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:4px;"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>
              ${escapeHtml(item.merge_status_detail)}
            </div>`;
          } else {
            conflictWarning = `<div class="pr-merge-detail">${escapeHtml(item.merge_status_detail)}</div>`;
          }

          const changesHtml = (item.changes && item.changes.length > 0)
            ? `<ul class="pr-changes-list">${item.changes.map(c => `<li>${escapeHtml(c)}</li>`).join('')}</ul>`
            : '<p class="pr-text" style="color: var(--text-muted);">No structured changes captured.</p>';
          const supersededHtml = item.superseded_by_pr_number
            ? `<div class="pr-merge-detail" style="color: var(--status-checking-fg);">
                Superseded by resolved PR <a href="${item.superseded_by_pr_url}" target="_blank" style="color: inherit; font-weight: 600;">#${item.superseded_by_pr_number}</a>
              </div>`
            : '';

          return `
            <div class="card pr-item">
              <div class="pr-item-header">
                <div class="pr-item-header-left">
                  ${hasPr ? `<input type="checkbox" class="pr-checkbox" data-id="${item.task_id}" ${isSelected ? 'checked' : ''}>` : '<span style="width: 16px;"></span>'}
                  <div>
                    <div class="pr-meta">
                      <span class="pr-repo">${escapeHtml(item.repository)}</span>
                      <span>•</span>
                      <span>Task #${item.task_id.substring(0,8)}...</span>
                    </div>
                    <a href="${titleUrl}" target="_blank" class="pr-title">
                      ${escapeHtml(item.title)} <span style="color: var(--text-muted); font-weight: normal;">${escapeHtml(titleNumber)}</span>
                    </a>
                  </div>
                </div>
                <div>
                  <span class="badge ${item.merge_status}">${escapeHtml(item.merge_status_label)}</span>
                </div>
              </div>
              
              <div class="pr-body">
                <div class="pr-section">
                  <div class="pr-section-title">Root Cause</div>
                  <div class="pr-text">${escapeHtml(item.root_cause || 'No root cause summary captured.')}</div>
                </div>
                
                <div class="pr-section">
                  <div class="pr-section-title">Changes</div>
                  ${changesHtml}
                </div>

                <div class="pr-section">
                  <div class="pr-section-title">${hasPr ? 'Mergeability' : 'Task Status'}</div>
                  ${conflictWarning}
                  ${supersededHtml}
                </div>

                <details class="diff-panel">
                  <summary>View Raw Diff</summary>
                  <pre class="diff-content"><code>${escapeHtml(item.diff || 'No diff captured.')}</code></pre>
                </details>
              </div>

              <div class="pr-actions">
                <a href="${titleUrl}" target="_blank" style="text-decoration:none;">
                  <button type="button">${hasPr ? 'View PR on GitHub' : 'View Issue on GitHub'}</button>
                </a>
                ${hasPr ? `<button type="button" class="success" onclick="handleMerge('${item.task_id}')" 
                  ${(isConflict || item.superseded_by_pr_number) ? 'disabled title="This PR cannot be merged directly"' : ''}>
                  Merge PR
                </button>` : ''}
                ${isConflict && !item.superseded_by_pr_number ? `
                  <button type="button" class="primary" onclick="handleResolveConflict('${item.task_id}')">
                    Resolve Conflict
                  </button>
                ` : ''}
              </div>
            </div>
          `;
        }).join('');

        // Re-attach listeners
        document.querySelectorAll('.pr-checkbox').forEach(cb => {
          cb.addEventListener('change', (e) => {
            const id = e.target.dataset.id;
            if (e.target.checked) state.selected.add(id);
            else state.selected.delete(id);
            updateSidebar();
          });
        });

      } catch (err) {
        listEl.innerHTML = `<div class="empty-state" style="color: var(--status-conflict-fg)">Failed to load PRs. Error: ${err.message}</div>`;
      }
    }

    window.handleMerge = async (taskId) => {
      const btn = document.querySelector(`button[onclick="handleMerge('${taskId}')"]`);
      const originalText = btn.textContent;
      btn.textContent = 'Merging...';
      btn.disabled = true;

      try {
        const res = await fetch(`/dashboard/prs/${taskId}/merge`, { method: 'POST' });
        const payload = await res.json();
        if (res.ok) {
          alert(`Merged Successfully! SHA: ${payload.sha || 'ok'}`);
          loadPRs(); // Reload list
        } else {
          alert(`Merge Failed: ${payload.detail || JSON.stringify(payload)}`);
          btn.textContent = originalText;
          btn.disabled = false;
        }
      } catch (err) {
        alert('Request failed');
        btn.textContent = originalText;
        btn.disabled = false;
      }
    };

    window.handleResolveConflict = async (taskId) => {
      const guidance = document.getElementById('guidance').value;
      const res = await fetch(`/dashboard/prs/${taskId}/resolve-conflict`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ guidance })
      });
      const payload = await res.json();
      if (res.ok) {
        alert(`Conflict resolution task queued: ${payload.id}`);
        loadPRs();
      } else {
        alert(`Conflict resolution failed: ${payload.detail || JSON.stringify(payload)}`);
      }
    };

    document.getElementById('integrateBtn').addEventListener('click', async (e) => {
      const btn = e.target;
      const originalText = btn.textContent;
      
      const taskIds = Array.from(state.selected);
      const guidance = document.getElementById('guidance').value;

      btn.textContent = 'Queuing Task...';
      btn.disabled = true;

      try {
        const res = await fetch('/dashboard/integrations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_ids: taskIds, guidance })
        });
        const payload = await res.json();
        
        if (res.ok) {
          alert(`Integration Task Queued: ${payload.id}`);
          // Clear selection after success
          state.selected.clear();
          document.querySelectorAll('.pr-checkbox').forEach(cb => cb.checked = false);
          document.getElementById('guidance').value = '';
          updateSidebar();
        } else {
          alert(`Integration Failed: ${payload.detail || JSON.stringify(payload)}`);
          btn.textContent = originalText;
          btn.disabled = false;
        }
      } catch (err) {
        alert('Request failed');
        btn.textContent = originalText;
        btn.disabled = false;
      }
    });

    document.getElementById('refreshBtn').addEventListener('click', loadPRs);

    // Initial load
    loadPRs();
  </script>
</body>
</html>"""
    return Response(content=html, media_type="text/html")


@router.get("/metrics")
def agent_metrics(db: Session = Depends(get_db)) -> dict:
    tasks = list(
        db.scalars(
            select(Task).options(
                selectinload(Task.attempts),
                selectinload(Task.artifacts),
            )
        )
    )
    return build_agent_metrics(tasks)


@router.get("/prs")
async def list_pr_dashboard_items(db: Session = Depends(get_db)) -> list[dict]:
    tasks = list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.repository), selectinload(Task.issue), selectinload(Task.artifacts), selectinload(Task.attempts))
            .order_by(Task.updated_at.desc())
            .limit(50)
        )
    )
    items: list[dict] = []
    auth_service = GitHubAuthService()
    token_cache: dict[int, str] = {}
    pr_service_cache: dict[int, GitHubPullRequestService] = {}
    for task in tasks:
        model_response = get_artifact_content(task, TaskArtifactType.model_response)
        diff_artifact = get_artifact_content(task, TaskArtifactType.diff)
        pr_body_artifact = get_artifact_content(task, TaskArtifactType.pr_body)
        pr_body = pr_body_artifact.get("body") if isinstance(pr_body_artifact, dict) else ""
        summary = model_response.get("summary") if isinstance(model_response, dict) and isinstance(model_response.get("summary"), dict) else {}
        diff_text = diff_artifact.get("diff") if isinstance(diff_artifact, dict) else ""
        changes = _normalize_changes(summary.get("patch_plan"))
        if not changes:
            changes = _normalize_changes(_extract_changes_from_pr_body(pr_body))
        if not changes:
            changes = _summarize_diff_changes(diff_text)
        root_cause = summary.get("root_cause") if isinstance(summary, dict) else ""
        if _is_generic_root_cause(root_cause):
            root_cause = _extract_section_from_pr_body(pr_body, "## Root Cause")
        if _is_generic_root_cause(root_cause):
            root_cause = _extract_section_from_pr_body(pr_body, "## Summary")
        if _is_generic_root_cause(root_cause):
            root_cause = _summarize_diff_root_cause(diff_text)

        if task.pr_number is None:
            status_label = "Failed" if task.status == TaskStatus.failed else task.status.value.replace("_", " ").title()
            status_detail = _failure_detail_for_task(task) if task.status == TaskStatus.failed else "Task accepted by the agent and is being processed."
            items.append(
                {
                    "task_id": task.id,
                    "repository": f"{task.repository.owner}/{task.repository.name}",
                    "title": task.issue.title,
                    "status": task.status.value,
                    "issue_number": task.issue.github_issue_number,
                    "issue_url": task.issue.html_url,
                    "pr_number": None,
                    "pr_url": "",
                    "root_cause": root_cause or status_detail,
                    "changes": changes,
                    "diff": diff_text,
                    "pr_body": pr_body,
                    "merge_status": "conflicting" if task.status == TaskStatus.failed else "checking",
                    "merge_status_label": status_label,
                    "merge_status_detail": status_detail,
                    "merge_conflict": False,
                    "superseded_by_task_id": None,
                    "superseded_by_pr_number": None,
                    "superseded_by_pr_url": "",
                }
            )
            continue

        installation_id = _installation_id_for_task(task)
        if installation_id is None:
            continue
        if installation_id not in token_cache:
            token_cache[installation_id] = await auth_service.get_installation_token(installation_id)
            pr_service_cache[installation_id] = GitHubPullRequestService(token_cache[installation_id])
        pr_payload = await pr_service_cache[installation_id].get_pull_request(
            task.repository.owner,
            task.repository.name,
            task.pr_number,
        )
        if pr_payload.get("state") != "open":
            continue
        merge_status, merge_status_label, merge_conflict = _merge_status_from_payload(pr_payload)
        resolution_link = _resolution_link_for_task(task)
        items.append(
            {
                "task_id": task.id,
                "repository": f"{task.repository.owner}/{task.repository.name}",
                "title": task.issue.title,
                "status": task.status.value,
                "issue_number": task.issue.github_issue_number,
                "issue_url": task.issue.html_url,
                "pr_number": task.pr_number,
                "pr_url": f"https://github.com/{task.repository.owner}/{task.repository.name}/pull/{task.pr_number}",
                "root_cause": root_cause,
                "changes": changes,
                "diff": diff_text,
                "pr_body": pr_body,
                "merge_status": merge_status,
                "merge_status_label": merge_status_label,
                "merge_status_detail": (
                    "This PR conflicts with the current base branch. It needs a manual rebase or an integration task."
                    if merge_conflict
                    else (
                        {
                            "clean": "GitHub reports this PR is ready to merge without conflicts.",
                            "checking": "GitHub is still calculating mergeability, please wait a moment.",
                            "behind": "This PR is behind the base branch and may need an update.",
                            "blocked": "This PR is blocked by required checks or branch rules.",
                            "unknown": "GitHub has not provided a clear mergeability result yet.",
                        }.get(merge_status, "GitHub has not provided a clear mergeability result yet.")
                    )
                ),
                "merge_conflict": merge_conflict,
                "superseded_by_task_id": resolution_link.get("resolved_task_id"),
                "superseded_by_pr_number": resolution_link.get("resolved_pr_number"),
                "superseded_by_pr_url": (
                    f"https://github.com/{task.repository.owner}/{task.repository.name}/pull/{resolution_link['resolved_pr_number']}"
                    if resolution_link.get("resolved_pr_number")
                    else ""
                ),
            }
        )
    return items


@router.post("/integrations")
def create_integration(request: IntegrationRequest, db: Session = Depends(get_db)) -> dict:
    tasks = list(
        db.scalars(
            select(Task)
            .where(Task.id.in_(request.task_ids))
            .options(selectinload(Task.repository), selectinload(Task.issue), selectinload(Task.artifacts), selectinload(Task.attempts))
        )
    )
    if len(tasks) != len(request.task_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found")
    task_map = {task.id: task for task in tasks}
    tasks = [task_map[task_id] for task_id in request.task_ids]
    if any(task.pr_number is None for task in tasks):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="integration_requires_pr_backed_tasks")
    integration_task = create_integration_task(db, tasks, request.guidance)
    return {"id": integration_task.id, "status": integration_task.status.value}


@router.post("/prs/{task_id}/resolve-conflict")
def resolve_conflict(task_id: str, request: ConflictResolutionRequest, db: Session = Depends(get_db)) -> dict:
    task = db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .options(selectinload(Task.repository), selectinload(Task.issue), selectinload(Task.artifacts), selectinload(Task.attempts))
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found")
    if task.pr_number is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="task_has_no_pr")
    resolution_link = _resolution_link_for_task(task)
    if resolution_link.get("resolved_task_id"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conflict_resolution_already_queued")
    resolution_task = create_conflict_resolution_task(db, task, request.guidance)
    return {"id": resolution_task.id, "status": resolution_task.status.value}


@router.post("/prs/{task_id}/merge")
async def merge_pr(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .options(selectinload(Task.repository), selectinload(Task.artifacts))
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found")
    if task.pr_number is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="task_has_no_pr")

    raw_webhook = get_artifact_content(task, TaskArtifactType.raw_webhook)
    installation_id = ((raw_webhook or {}).get("installation") or {}).get("id")
    if installation_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="installation_id_missing")

    auth_service = GitHubAuthService()
    installation_token = await auth_service.get_installation_token(installation_id)
    pr_service = GitHubPullRequestService(installation_token)
    return await pr_service.merge_pull_request(task.repository.owner, task.repository.name, task.pr_number)
