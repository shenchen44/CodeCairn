from app.services.sandbox.repo_config import load_repo_config


def test_load_repo_config_defaults(workspace_tmp_dir) -> None:
    config = load_repo_config(workspace_tmp_dir)
    assert config.language == "python"
    assert config.max_changed_files == 5


def test_load_repo_config_file(workspace_tmp_dir) -> None:
    (workspace_tmp_dir / ".agent.yml").write_text(
        "language: python\n"
        "test_command: pytest -q\n"
        "install_command: pip install -r requirements.txt\n"
        "allowed_paths:\n"
        "  - app/\n"
        "blocked_paths:\n"
        "  - migrations/\n"
        "max_changed_files: 2\n"
        "max_diff_lines: 10\n",
        encoding="utf-8",
    )
    config = load_repo_config(workspace_tmp_dir)
    assert config.allowed_paths == ["app/"]
    assert config.blocked_paths == ["migrations/"]
    assert config.max_changed_files == 2


def test_detects_javascript_repository_without_python_restriction(
    workspace_tmp_dir,
) -> None:
    (workspace_tmp_dir / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}',
        encoding="utf-8",
    )

    config = load_repo_config(workspace_tmp_dir)

    assert config.language == "typescript/javascript"
    assert config.install_command == "npm install"
    assert config.test_command == "npm test"
    assert config.allowed_paths == [""]
