"""CI uses the same Postgres image as docker-compose.yml (#52).

Sentinel starts the database with ``docker compose`` itself, so there is one
pinned image. This test fails if a workflow or the Dependabot config ever pins
a different ``postgres`` image, or if Sentinel stops using compose.
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
IMAGE_REF = re.compile(r"postgres:[\w.-]+(?:@sha256:[0-9a-f]{64})?")


def compose_image() -> str:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    image: str = compose["services"]["postgres"]["image"]
    return image


def test_compose_image_is_pinned_by_digest() -> None:
    assert re.fullmatch(r"postgres:[\w.-]+@sha256:[0-9a-f]{64}", compose_image())


def test_no_other_postgres_image_in_github_config() -> None:
    expected = compose_image()
    found = {
        f"{path.relative_to(REPO)}: {ref}"
        for path in sorted((REPO / ".github").rglob("*.y*ml"))
        for ref in IMAGE_REF.findall(path.read_text())
        if ref != expected
    }
    assert found == set(), f"must equal docker-compose.yml's {expected}"


def test_sentinel_starts_postgres_from_compose() -> None:
    workflow = (REPO / ".github/workflows/sentinel.yml").read_text()
    assert "docker compose up" in workflow
    assert "AETHER_DB_TESTS=1" in workflow


def test_detector_catches_a_different_digest() -> None:
    other = "postgres:18.7-alpine@sha256:" + "0" * 64
    assert IMAGE_REF.findall(f"image: {other}") == [other]
    assert other != compose_image()
