import stat
from pathlib import Path

import pytest

from reporewind.errors import RecipeError
from reporewind.models import RepoRef
from reporewind.recipes import (
    Detection,
    MemoryTreeSource,
    RecipeFile,
    RecipeStore,
    detect_recipe,
    dump_recipe_file,
    load_recipe_file,
    parse_recipe_file,
    recipe_hash,
)
from reporewind.recipes.store import HEADER, DetectedBlock

REPO = RepoRef.parse("Example/Calc")
COMMIT = "a" * 40
COMMITTED_RECIPES = Path(__file__).resolve().parent.parent / "recipes"


def detection(**fields: object) -> Detection:
    base: dict[str, object] = {"python": ">=3.8", "install": "editable", "env": {"A": "1"}}
    return Detection(
        commit=COMMIT,
        backend="setuptools",
        sources=("pyproject.toml",),
        notes=("pytest: conftest.py",),
        fields={**base, **fields},
    )


def recipe_file(overrides: dict[str, object] | None = None) -> RecipeFile:
    return RecipeFile(
        repo=REPO,
        detected=DetectedBlock.from_detection(detection()),
        overrides=overrides or {},
    )


def test_yaml_round_trip() -> None:
    original = recipe_file({"test_dependencies": ["pytest<8"]})
    text = dump_recipe_file(original)
    assert text.startswith(HEADER)
    assert text.isascii()
    assert "repo: Example/Calc\n" in text
    parsed = parse_recipe_file(text)
    assert parsed == original
    assert parsed.recipe_hash() == original.recipe_hash()
    assert dump_recipe_file(parsed) == text


def test_overrides_deep_merge_over_detected_values() -> None:
    plain = recipe_file()
    merged = recipe_file(
        {"env": {"B": "2", "A": None}, "python": None, "extras": ["test"], "pre_install": ["x"]}
    )
    recipe = merged.recipe()
    assert recipe.env == {"B": "2"}
    assert recipe.python is None
    assert recipe.extras == ("test",)
    assert merged.overridden == ("env", "extras", "pre_install", "python")
    assert merged.recipe_hash() != plain.recipe_hash()
    assert merged.recipe_hash() == recipe_hash(recipe)
    # Detected values are untouched by the merge.
    assert merged.detected.recipe["env"] == {"A": "1"}


def test_framework_override_brings_its_default_command() -> None:
    recipe = recipe_file({"test_framework": "unittest"}).recipe()
    assert recipe.test_command[:3] == ("python", "-m", "unittest")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"pyhton": ">=3"}, "pyhton: Extra inputs are not permitted"),
        ({"python": 3.1}, "python: Input should be a valid string"),
        ({"env": {"DEBUG": True}}, "env.DEBUG: Input should be a valid string"),
        ({"install": "requirements"}, "needs at least one requirements file"),
    ],
)
def test_invalid_overrides_are_reported_with_their_location(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(RecipeError, match="detected \\+ overrides") as info:
        recipe_file(overrides).recipe()
    assert message in str(info.value)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("repo: a/b\nrepo: c/d\n", "found duplicate key 'repo'"),
        ("repo: [unclosed\n", "not valid YAML"),
        ("- a\n- b\n", "expected a mapping at the top level"),
        ("repo: a/b\nschema_version: 2\n", "schema_version: Input should be 1"),
        ("repo: a/b\nextra: 1\n", "extra: Extra inputs are not permitted"),
        ("repo: not a repo\n", "repo:"),
        ("repo: a/b\noverrides:\n  python: '>=3.x'\n", "invalid Python version specifier"),
        ("repo: a/b\n? [1, 2]\n: x\n", "not valid YAML"),
        ("repo: a/b\ndetected:\n  commit: xyz\n", "detected.commit"),
    ],
)
def test_invalid_files_are_rejected(text: str, message: str) -> None:
    with pytest.raises(RecipeError, match="<recipe>") as info:
        parse_recipe_file(text)
    assert message in str(info.value)


def test_minimal_file_uses_schema_defaults() -> None:
    parsed = parse_recipe_file("repo: a/b\n")
    assert parsed.recipe().test_command == ("python", "-m", "pytest", "-rA")
    assert "commit" not in parsed.to_data()["detected"]


def test_load_checks_the_file_name_and_repository(tmp_path: Path) -> None:
    good = tmp_path / "example__calc.yaml"
    good.write_text(dump_recipe_file(recipe_file()))
    assert load_recipe_file(good, expected=REPO).repo == REPO
    with pytest.raises(RecipeError, match="is the recipe for Example/Calc, not other/repo"):
        load_recipe_file(good, expected=RepoRef.parse("other/repo"))
    renamed = tmp_path / "calc.yaml"
    renamed.write_text(good.read_text())
    with pytest.raises(RecipeError, match=r"must be named example__calc\.yaml"):
        load_recipe_file(renamed)
    binary = tmp_path / "bad.yaml"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(RecipeError, match="cannot read recipe file"):
        load_recipe_file(binary)


def test_store_save_load_and_merge_detection(tmp_path: Path) -> None:
    store = RecipeStore(tmp_path / "recipes")
    assert store.paths() == ()
    with pytest.raises(RecipeError, match="run `reporewind recipe detect` first"):
        store.load(REPO)

    first = store.merge_detection(REPO, detection())
    path = store.save(first)
    assert path == tmp_path / "recipes" / "example__calc.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert [p.name for p in (tmp_path / "recipes").iterdir()] == ["example__calc.yaml"]

    edited = RecipeFile(repo=REPO, detected=first.detected, overrides={"env": {"B": "2"}})
    store.save(edited)
    redetected = store.merge_detection(REPO, detection(python=">=3.10"))
    assert redetected.overrides == {"env": {"B": "2"}}
    assert redetected.recipe().python == ">=3.10"
    assert redetected.recipe().env == {"A": "1", "B": "2"}
    assert store.paths() == (path,)

    # A detection that makes the kept overrides invalid is refused.
    store.save(RecipeFile(repo=REPO, detected=first.detected, overrides={"install": "none"}))
    with pytest.raises(RecipeError, match="extras need an installed project"):
        store.merge_detection(REPO, detection(extras=["test"]))


def test_detection_to_file_round_trip() -> None:
    found = detect_recipe(
        MemoryTreeSource(
            {"setup.py": "from setuptools import setup\nsetup(python_requires='>=3.7')\n"},
            commit=COMMIT,
        )
    )
    saved = parse_recipe_file(dump_recipe_file(RecipeFile(repo=REPO).with_detection(found)))
    assert saved.detected.commit == COMMIT
    assert saved.recipe() == found.recipe()


@pytest.mark.skipif(not COMMITTED_RECIPES.is_dir(), reason="no committed recipes")
def test_committed_recipes_are_valid() -> None:
    paths = RecipeStore(COMMITTED_RECIPES).paths()
    assert paths
    for path in paths:
        assert load_recipe_file(path).recipe_hash().startswith("sha256:")


def test_failed_save_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecipeStore(tmp_path)

    def refuse(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        store.save(recipe_file())
    assert list(tmp_path.iterdir()) == []
