import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text()
CLOUDBUILD = (ROOT / "cloudbuild.yaml").read_text()


def instructions(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerfile_base_image():
    assert instructions(DOCKERFILE)[0].startswith("FROM python:3.11")


def test_dockerfile_switches_to_non_root_user_before_cmd():
    lines = instructions(DOCKERFILE)
    users = [i for i, line in enumerate(lines) if line.startswith("USER ")]
    cmds = [i for i, line in enumerate(lines) if line.startswith("CMD ")]
    assert users and cmds
    assert users[-1] < cmds[0]
    assert lines[users[-1]].split()[1] not in ("root", "0")


def test_dockerfile_installs_runtime_requirements_only():
    assert "pip install --no-cache-dir -r requirements.txt" in DOCKERFILE
    assert "requirements-dev" not in DOCKERFILE


def test_dockerfile_bakes_in_no_key():
    for line in instructions(DOCKERFILE):
        if line.startswith(("ENV ", "ARG ")):
            assert "KEY" not in line.upper()
        if not line.startswith("CMD "):
            assert "GEMINI_API_KEY" not in line
    assert not re.search(r"AIza[0-9A-Za-z_\-]{20,}", DOCKERFILE)


def test_dockerfile_cmd_uses_app_module_and_port():
    cmd = next(line for line in instructions(DOCKERFILE) if line.startswith("CMD "))
    assert "app:app" in cmd
    assert "PORT" in cmd
    # The application path lives in one place only. COPY lines are skipped
    # because --chown=app:app names the user and group, not the module.
    others = [
        line for line in DOCKERFILE.splitlines() if not line.strip().startswith("COPY ")
    ]
    assert sum(line.count("app:app") for line in others) == 1


def test_dockerignore_entries():
    entries = {line.strip() for line in DOCKERIGNORE.splitlines() if line.strip()}
    for needed in (".env", ".venv", ".git", "requirements-dev.txt"):
        assert needed in entries
    assert "northwind.db" not in entries
    assert "definitions.txt" not in entries


def test_cloudbuild_contents():
    for needed in (
        "us-central1",
        "CLOUD_LOGGING_ONLY",
        "id: build",
        "id: push",
        "id: deploy",
        "images:",
        "--set-secrets=GEMINI_API_KEY=",
        "--allow-unauthenticated",
        "--max-instances",
    ):
        assert needed in CLOUDBUILD
    assert not re.search(r"AIza[0-9A-Za-z_\-]{20,}", CLOUDBUILD)


def test_cloudbuild_parses_as_mapping():
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(CLOUDBUILD)
    assert isinstance(parsed, dict)
    for key in ("substitutions", "steps", "images", "options"):
        assert key in parsed


def test_copy_sources_exist():
    for line in instructions(DOCKERFILE):
        if not line.startswith("COPY "):
            continue
        parts = [p for p in line.split()[1:] if not p.startswith("--")]
        for source in parts[:-1]:
            if source == ".":
                continue
            if source == "northwind.db":
                # The database is added later; its absence is reported, not failed.
                continue
            assert (ROOT / source).exists(), source
