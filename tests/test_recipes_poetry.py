import pytest

from reporewind.recipes.poetry import poetry_constraint, poetry_requirement, python_markers


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        ("^1.2.3", "<2,>=1.2.3"),
        ("^1.2", "<2,>=1.2"),
        ("^1", "<2,>=1"),
        ("^0.2.3", "<0.3,>=0.2.3"),
        ("^0.0.3", "<0.0.4,>=0.0.3"),
        ("^0.0", "<0.1,>=0.0"),
        ("^0", "<1,>=0"),
        ("~1.2.3", "<1.3,>=1.2.3"),
        ("~1.2", "<1.3,>=1.2"),
        ("~1", "<2,>=1"),
        ("~=1.4", "~=1.4"),
        ("1.2.*", "==1.2.*"),
        ("1.2.3", "==1.2.3"),
        ("=1.2.3", "==1.2.3"),
        (">= 1.2, < 1.5", "<1.5,>=1.2"),
        (">=1.2 <1.5", "<1.5,>=1.2"),
        ("!=1.3", "!=1.3"),
        ("*", ""),
        ("^3.7", "<4,>=3.7"),
    ],
)
def test_poetry_constraints_translate_to_pep_440(constraint: str, expected: str) -> None:
    assert poetry_constraint(constraint) == expected


@pytest.mark.parametrize("constraint", ["^2.7 || ^3.5", "^abc", ">=1.x"])
def test_untranslatable_constraints_raise(constraint: str) -> None:
    with pytest.raises(ValueError, match=r"constraint|version"):
        poetry_constraint(constraint)


def test_python_markers() -> None:
    assert python_markers("^3.7") == 'python_version < "4" and python_version >= "3.7"'
    assert python_markers("*") == ""


def test_poetry_requirements() -> None:
    assert poetry_requirement("pytest", "^6.0") == "pytest<7,>=6.0"
    table = {"version": "~2.1", "extras": ["toml"], "python": "<3.8", "markers": "os_name == 'nt'"}
    assert poetry_requirement("coverage", table) == (
        "coverage[toml]<2.2,>=2.1; python_version < \"3.8\" and (os_name == 'nt')"
    )
    assert poetry_requirement("mock", {"python": "*"}) == "mock"
    with pytest.raises(ValueError, match="git source"):
        poetry_requirement("lib", {"git": "https://example.invalid/lib.git"})
    with pytest.raises(ValueError, match="multiple-constraint"):
        poetry_requirement("lib", [{"version": "1"}, {"version": "2"}])


def test_patch_level_python_bounds_use_the_full_version() -> None:
    from packaging.markers import Marker

    typed_ast = poetry_requirement("typed-ast", {"version": "^1.4", "python": ">=3.6.2,<3.8"})
    marker = Marker(typed_ast.split(";", 1)[1])
    assert marker.evaluate({"python_version": "3.6", "python_full_version": "3.6.15"})
    assert not marker.evaluate({"python_version": "3.6", "python_full_version": "3.6.1"})
    assert not marker.evaluate({"python_version": "3.8", "python_full_version": "3.8.0"})
    not_390 = Marker(poetry_requirement("foo", {"version": "*", "python": "!=3.9.0"}).split(";")[1])
    assert not_390.evaluate({"python_version": "3.9", "python_full_version": "3.9.7"})
    assert not not_390.evaluate({"python_version": "3.9", "python_full_version": "3.9.0"})
    assert python_markers("~3.6.2") == ('python_version < "3.7" and python_full_version >= "3.6.2"')
    assert python_markers("3.9.*") == 'python_version == "3.9.*"'
