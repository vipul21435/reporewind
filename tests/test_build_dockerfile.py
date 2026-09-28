import os
from pathlib import Path

import pytest

from buildkit import DIGEST, LOCK, pin_for
from reporewind.build import render_dockerfile
from reporewind.build.dockerfile import UV_IMAGE
from reporewind.errors import BuildError
from reporewind.recipes.model import Recipe

GOLDEN = Path(__file__).parent / "golden"

RECIPES = {
    "editable_pytest": Recipe(),
    "package_unittest_native": Recipe(
        install="package",
        system_packages=("build-essential",),
        pre_install=("python -c 'print(1)'",),
        test_framework="unittest",
        test_command=("python", "-m", "unittest", "discover", "-v", "-s", "tests", "-t", "."),
        env={"TZ": "UTC", "LC_ALL": "C.UTF-8"},
    ),
    "requirements_only": Recipe(install="requirements", requirements_files=("requirements.txt",)),
}


@pytest.mark.parametrize("name", sorted(RECIPES))
def test_dockerfile_matches_golden(name: str) -> None:
    recipe = RECIPES[name]
    text = render_dockerfile(recipe, pin_for(recipe), LOCK)
    golden = GOLDEN / f"{name}.Dockerfile"
    if os.environ.get("REPOREWIND_UPDATE_GOLDEN") == "1":
        golden.write_text(text)
    assert text == golden.read_text()
    assert text.isascii()


def test_dockerfile_is_pinned_non_root_and_deterministic() -> None:
    recipe = Recipe()
    text = render_dockerfile(recipe, pin_for(recipe), LOCK)
    assert text == render_dockerfile(recipe, pin_for(recipe), LOCK)
    assert f"FROM python:3.8-slim@{DIGEST}\n" in text
    assert f"FROM {UV_IMAGE} AS uv\n" in text
    assert "@sha256:" in UV_IMAGE
    assert text.rstrip().splitlines()[-2] == "USER 10001:10001"
    assert "--require-hashes -r /opt/reporewind/requirements.lock" in text
    assert "--exclude-newer 2021-05-11T19:02:03Z --no-deps -e ." in text
    assert "LABEL project=reporewind" in text
    assert "git clone" not in text
    assert "ADD " not in text


def test_lock_without_hashes_does_not_require_them() -> None:
    lock = "pytest==6.2.4\n"
    recipe = Recipe()
    text = render_dockerfile(recipe, pin_for(recipe, lock), lock)
    assert "--require-hashes" not in text
    assert "-r /opt/reporewind/requirements.lock" in text


def test_inputs_must_match_the_pin() -> None:
    recipe = Recipe()
    pin = pin_for(recipe)
    with pytest.raises(BuildError, match="changed since it was pinned"):
        render_dockerfile(Recipe(env={"A": "1"}), pin, LOCK)
    with pytest.raises(BuildError, match="does not match the sha256 recorded"):
        render_dockerfile(recipe, pin, LOCK + "extra==1\n")
