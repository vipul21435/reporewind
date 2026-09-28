import json

import pytest
from pydantic import ValidationError

from reporewind.recipes import (
    DEFAULT_TEST_COMMANDS,
    Framework,
    InstallMode,
    Recipe,
    canonical_json,
    merge_patch,
    recipe_hash,
)
from reporewind.recipes.model import command_runner


def test_defaults_fill_the_framework_test_command() -> None:
    recipe = Recipe()
    assert recipe.install is InstallMode.EDITABLE
    assert recipe.test_framework is Framework.PYTEST
    assert recipe.test_command == ("python", "-m", "pytest", "-rA")
    unittest = Recipe(test_framework=Framework.UNITTEST)
    assert unittest.test_command == DEFAULT_TEST_COMMANDS[Framework.UNITTEST]
    explicit = Recipe.model_validate({"test_framework": "unittest", "test_command": ["tox"]})
    assert explicit.test_command == ("tox",)


def test_values_are_normalized() -> None:
    recipe = Recipe.model_validate(
        {
            "python": " >=3.8, <4 ,!=3.9.* ",
            "extras": ["Tests", "docs", "tests"],
            "test_dependencies": ["pytest >= 6 ; python_version<'3.8'", "hypothesis"],
            "system_packages": ["libxml2-dev", "build-essential", "libxml2-dev"],
            "env": {"B": "2", "A": "1"},
            "pre_install": ["pip install -U pip", "echo ready"],
        }
    )
    assert recipe.pre_install == ("pip install -U pip", "echo ready")
    assert recipe.python == "!=3.9.*,<4,>=3.8"
    assert recipe.extras == ("docs", "tests")
    assert recipe.test_dependencies == ('pytest>=6; python_version < "3.8"', "hypothesis")
    assert recipe.system_packages == ("build-essential", "libxml2-dev")
    assert list(recipe.env) == ["A", "B"]
    assert Recipe(python="  ").python is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"python": ">=3.x"}, "invalid Python version specifier"),
        ({"extras": ["-bad"]}, "invalid extra name"),
        ({"test_dependencies": ["pytest >>= 6"]}, "invalid requirement"),
        ({"test_dependencies": ["pytest", "pytest"]}, "duplicate test dependency"),
        ({"requirements_files": ["a.txt", "a.txt"]}, "duplicate requirements file"),
        ({"requirements_files": ["../a.txt"]}, "'..'"),
        ({"system_packages": ["Build Essential"]}, "invalid Debian package name"),
        ({"pre_install": ["echo a\necho b"]}, "single line"),
        ({"pre_install": ["  "]}, "must not be blank"),
        ({"test_command": ["python", ""]}, "non-empty"),
        ({"env": {"1BAD": "x"}}, "invalid environment variable name"),
        ({"env": {"OK": "a\x00b"}}, "contains NUL"),
        ({"install": "requirements"}, "needs at least one requirements file"),
        ({"install": "none", "extras": ["test"]}, "extras need an installed project"),
        ({"test_framework": "nose"}, "test_framework"),
        ({"unknown": 1}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_recipes_are_rejected(data: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Recipe.model_validate(data)


def test_merge_patch_follows_rfc_7396() -> None:
    target = {"a": "b", "c": {"d": "e", "f": "g"}, "l": [1, 2]}
    patch = {"a": "z", "c": {"f": None}, "l": [3], "n": {"x": None}}
    merged = merge_patch(target, patch)
    assert merged == {"a": "z", "c": {"d": "e"}, "l": [3], "n": {}}
    assert target == {"a": "b", "c": {"d": "e", "f": "g"}, "l": [1, 2]}
    assert merge_patch({"a": 1}, ["x"]) == ["x"]
    assert merge_patch("scalar", {"a": 1}) == {"a": 1}
    # Examples from RFC 7396, appendix A.
    assert merge_patch({"a": "b"}, {"a": None}) == {}
    assert merge_patch({"a": [{"b": "c"}]}, {"a": [1]}) == {"a": [1]}
    assert merge_patch({"e": None}, {"a": 1}) == {"e": None, "a": 1}


def test_merge_patch_result_does_not_alias_the_patch() -> None:
    patch = {"env": {"A": "1"}, "extras": ["test"]}
    merged = merge_patch({}, patch)
    merged["extras"].append("docs")
    merged["env"]["B"] = "2"
    assert patch == {"env": {"A": "1"}, "extras": ["test"]}


def test_hash_is_stable_and_order_independent() -> None:
    one = Recipe.model_validate(
        {"python": ">=3.8,<4", "extras": ["tests", "docs"], "env": {"A": "1", "B": "2"}}
    )
    two = Recipe.model_validate(
        {"env": {"B": "2", "A": "1"}, "extras": ["docs", "tests"], "python": "<4, >=3.8"}
    )
    assert canonical_json(one) == canonical_json(two)
    assert recipe_hash(one) == recipe_hash(two)
    assert recipe_hash(one) != recipe_hash(Recipe(python=">=3.9,<4"))
    # Pinned: a change here means every stored recipe hash changes too.
    assert recipe_hash(Recipe()) == (
        "sha256:c7b52b3480aafabc2cfc9cb102a092ddda770e72e00ec965733d84a62f48c8f5"
    )


def test_canonical_json_shape() -> None:
    payload = json.loads(canonical_json(Recipe(python=">=3.8")))
    assert payload["schema_version"] == 1
    assert payload["recipe"]["python"] == ">=3.8"
    assert payload["recipe"]["test_command"] == ["python", "-m", "pytest", "-rA"]
    assert " " not in canonical_json(Recipe()).replace("python -m", "")


@pytest.mark.parametrize(
    ("command", "runner"),
    [
        (["pytest", "-x"], Framework.PYTEST),
        (["/usr/bin/py.test"], Framework.PYTEST),
        (["python", "-W", "error", "-m", "pytest"], Framework.PYTEST),
        (["python", "-m", "unittest", "discover"], Framework.UNITTEST),
        (["python", "-m", "tox"], None),
        (["make", "test"], None),
        ([], None),
    ],
)
def test_command_runner(command: list[str], runner: Framework | None) -> None:
    assert command_runner(command) is runner


def test_recipe_rejects_a_command_for_the_other_framework() -> None:
    with pytest.raises(ValidationError, match="test_command runs unittest"):
        Recipe(test_framework=Framework.PYTEST, test_command=("python", "-m", "unittest"))
    custom = Recipe(test_framework=Framework.UNITTEST, test_command=("make", "check"))
    assert custom.test_command == ("make", "check")
