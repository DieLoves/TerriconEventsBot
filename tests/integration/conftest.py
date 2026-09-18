from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from alembic import command
from alembic.config import Config


@contextmanager
def database_url_environment(url: str) -> Iterator[None]:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def run_alembic(url: str, operation: Callable[..., Any], *args: object) -> None:
    with database_url_environment(url):
        operation(Config("alembic.ini"), *args)


@pytest.fixture(scope="session")
def migrated_database_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    run_alembic(url, command.downgrade, "base")
    run_alembic(url, command.upgrade, "head")
    try:
        yield url
    finally:
        run_alembic(url, command.downgrade, "base")


@pytest.fixture
def alembic_runner(migrated_database_url: str) -> Callable[..., None]:
    def run(operation: Callable[..., Any], *args: object) -> None:
        run_alembic(migrated_database_url, operation, *args)

    return run
