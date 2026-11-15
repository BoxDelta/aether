import core


def test_core_package_has_version() -> None:
    assert core.__version__ == "0.1.0"
