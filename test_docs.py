import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
README = (ROOT / "README.md").read_text()
CLOUDBUILD = (ROOT / "cloudbuild.yaml").read_text()
DATABASE = ROOT / "northwind.db"

EXPECTED_SHA256 = "2f4f5c68dfcd33ba27373eae48c7a4869800c68095ee0f9f0da494f83382a877"
DOWNLOAD_URL = (
    "https://raw.githubusercontent.com/jpwhite3/northwind-SQLite3/main/dist/northwind.db"
)


@pytest.fixture(scope="module", autouse=True)
def database_unchanged():
    # Only the hash check depends on the file; the text checks must still run.
    if not DATABASE.exists():
        yield
        return
    before = hashlib.sha256(DATABASE.read_bytes()).hexdigest()
    yield
    assert hashlib.sha256(DATABASE.read_bytes()).hexdigest() == before


def test_readme_contents():
    for needed in (EXPECTED_SHA256, DOWNLOAD_URL, "GEMINI_API_KEY", "requirements-dev.txt"):
        assert needed in README
    assert not re.search(r"AIza[0-9A-Za-z_\-]{20,}", README)


def test_cloudbuild_resource_limits():
    for needed in ("--memory=1Gi", "--concurrency=4", "--max-instances=2"):
        assert needed in CLOUDBUILD
    for gone in ("--memory=512Mi", "--concurrency=20"):
        assert gone not in CLOUDBUILD


def test_cloudbuild_keeps_its_structure():
    for needed in (
        "us-central1",
        "CLOUD_LOGGING_ONLY",
        "id: build",
        "id: push",
        "id: deploy",
        "images:",
        "--set-secrets=GEMINI_API_KEY=",
        "--allow-unauthenticated",
    ):
        assert needed in CLOUDBUILD
