"""The guard that keeps tests away from the application database."""

import pytest

from tests.database import UnsafeTestDatabaseError, check_test_database_url, require_test_database_name

APP_URL = "postgresql+psycopg://user:secret@db:5432/certificates"


def test_accepts_a_separate_database_ending_in_test() -> None:
    url = check_test_database_url("postgresql+psycopg://user:secret@db:5432/certificates_test", APP_URL)

    assert url.database == "certificates_test"


def test_refuses_when_test_url_is_missing() -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="TEST_DATABASE_URL is not set"):
        check_test_database_url(None, APP_URL)


def test_refuses_the_application_database() -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="must end with '_test'"):
        check_test_database_url(APP_URL, APP_URL)


def test_refuses_same_name_even_through_a_different_host_alias() -> None:
    app_url = "postgresql+psycopg://user:secret@db:5432/shared_test"
    test_url = "postgresql+psycopg://user:secret@127.0.0.1:5432/shared_test"

    with pytest.raises(UnsafeTestDatabaseError, match="application database"):
        check_test_database_url(test_url, app_url)


@pytest.mark.parametrize("name", [None, "", "certificates", "postgres", "test", 'bad"name_test', "x; DROP_test "])
def test_destructive_helpers_refuse_non_test_names(name: str | None) -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        require_test_database_name(name)
