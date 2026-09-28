from pathlib import Path

import pytest

from reporewind.errors import InvalidPathError
from reporewind.gitops import Git
from reporewind.recipes import Framework, InstallMode
from reporewind.recipes.detect import Detection, detect_at, detect_recipe
from reporewind.recipes.source import GitTreeSource, MemoryTreeSource
from reporewind.testing import RepoFactory

UNITTEST_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n"
PYTEST_TEST = "import pytest\n\n\ndef test_x():\n    assert True\n"


def detect(files: dict[str, str | bytes]) -> Detection:
    return detect_recipe(MemoryTreeSource(files))


def test_pep_621_hatch_project() -> None:
    found = detect(
        {
            "pyproject.toml": """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "demo"
requires-python = ">=3.8"
dependencies = ["attrs>=20"]

[project.optional-dependencies]
Tests = ["pytest>=7", "hypothesis"]
docs = ["sphinx"]

[tool.hatch.envs.default]
dependencies = ["coverage[toml]", "{root:uri}/vendored"]
features = ["docs"]

[tool.hatch.envs.default.scripts]
cov = "pytest --cov {args}"
""",
            "src/demo/__init__.py": "",
            "tests/test_demo.py": PYTEST_TEST,
        }
    )
    assert found.backend == "hatch"
    assert found.sources == ("pyproject.toml",)
    assert found.fields == {
        "python": ">=3.8",
        "install": "editable",
        "extras": ["docs", "tests"],
        "test_framework": "pytest",
        "test_dependencies": ["coverage[toml]"],
    }
    assert any("uses tool-specific substitution" in note for note in found.notes)
    assert "pytest: pyproject.toml hatch env default runs pytest" in found.notes
    recipe = found.recipe()
    assert recipe.install is InstallMode.EDITABLE
    assert recipe.test_command == ("python", "-m", "pytest", "-rA")


def test_setuptools_pyproject_with_dependency_groups() -> None:
    found = detect(
        {
            "pyproject.toml": """
[build-system]
requires = ["setuptools>=61", "cython"]
build-backend = "setuptools.build_meta"

[project]
name = "demo"
requires-python = ">=3.9"

[dependency-groups]
Testing = ["pytest", {include-group = "cov"}, {include-group = "testing"}]
cov = ["coverage"]
dev = ["ruff"]

[tool.pytest.ini_options]
addopts = "-q"
""",
        }
    )
    assert found.backend == "setuptools"
    assert found.fields["test_dependencies"] == ["pytest", "coverage"]
    assert found.fields["system_packages"] == ["build-essential"]
    assert any("dependency group cycle" in note for note in found.notes)
    assert "pytest: pyproject.toml [tool.pytest]" in found.notes


def test_dev_group_is_the_fallback_for_test_dependencies() -> None:
    found = detect(
        {
            "pyproject.toml": '[project]\nname = "d"\n[dependency-groups]\ndev = ["pytest"]\n',
            "tests/test_a.py": PYTEST_TEST,
        }
    )
    assert found.backend == "setuptools"
    assert found.fields["test_dependencies"] == ["pytest"]
    assert "pytest is not declared anywhere; added unpinned (pinning dates it)" not in found.notes


def test_poetry_project() -> None:
    found = detect(
        {
            "pyproject.toml": """
[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"

[tool.poetry]
name = "demo"

[tool.poetry.dependencies]
python = "^3.8"
requests = "^2.25"

[tool.poetry.extras]
Socks = ["pysocks"]
"bad extra!" = []

[tool.poetry.group.test.dependencies]
pytest = "^7.0"
pytest-mock = {version = "~3.6", python = "<3.12"}
localpkg = {path = "../localpkg"}

[tool.poetry.group.docs.dependencies]
sphinx = "*"
""",
        }
    )
    assert found.backend == "poetry"
    assert found.fields["python"] == "<4,>=3.8"
    assert found.fields["install"] == "editable"
    assert found.fields["test_dependencies"] == [
        "pytest<8,>=7.0",
        'pytest-mock<3.7,>=3.6; python_version < "3.12"',
    ]
    assert "extras" not in found.fields
    assert any("localpkg is installed from a path source" in note for note in found.notes)
    assert any("invalid extra name 'bad extra!'" in note for note in found.notes)


def test_legacy_poetry_uses_package_install_and_dev_dependencies() -> None:
    found = detect(
        {
            "pyproject.toml": """
[build-system]
requires = ["poetry>=0.12"]
build-backend = "poetry.masonry.api"

[tool.poetry.dependencies]
python = "~2.7 || ^3.5"
pytest = {version = "^4.6", optional = true}

[tool.poetry.dev-dependencies]
pytest = "^4.6"
""",
        }
    )
    assert found.fields["install"] == "package"
    assert "python" not in found.fields
    assert found.fields["test_dependencies"] == ["pytest<5,>=4.6"]
    assert any("has no editable installs" in note for note in found.notes)
    assert any("was not translated" in note for note in found.notes)


def test_legacy_flit_metadata() -> None:
    found = detect(
        {
            "pyproject.toml": """
[build-system]
requires = ["flit"]
build-backend = "flit.buildapi"

[tool.flit.metadata]
module = "demo"
requires-python = ">=3.5"
requires = ["pytest"]

[tool.flit.metadata.requires-extra]
test = ["pytest", "testpath"]
""",
        }
    )
    assert found.backend == "flit"
    assert found.fields["python"] == ">=3.5"
    assert found.fields["install"] == "package"
    assert found.fields["extras"] == ["test"]
    assert "test_dependencies" not in found.fields


def test_rust_and_unknown_backends_are_reported() -> None:
    found = detect(
        {
            "pyproject.toml": """
[build-system]
requires = ["maturin>=1"]
build-backend = "maturin"
""",
        }
    )
    assert found.backend == "maturin"
    assert any("Rust toolchain" in note for note in found.notes)
    other = detect({"pyproject.toml": '[build-system]\nbuild-backend = "custom.backend"\n'})
    assert other.backend == "custom.backend"
    legacy = detect({"pyproject.toml": '[build-system]\nrequires = ["setuptools"]\n'})
    assert legacy.backend == "setuptools"


def test_setup_cfg() -> None:
    found = detect(
        {
            "setup.cfg": """
[metadata]
name = demo

[options]
python_requires = >=3.6
install_requires =
    six
    # a comment
    attrs  # trailing comment
tests_require =
    pytest-cov
    not a requirement !!

[options.extras_require]
testing =
    pytest
docs = sphinx

[tool:pytest]
testpaths = tests
""",
        }
    )
    assert found.backend == "setuptools"
    assert found.fields["python"] == ">=3.6"
    assert found.fields["extras"] == ["testing"]
    assert found.fields["test_dependencies"] == ["pytest-cov"]
    assert any("not a PEP 508 requirement" in note for note in found.notes)
    assert "pytest: setup.cfg [tool:pytest]" in found.notes


SETUP_PY = """
import os
raise RuntimeError("detection must never execute setup.py")
from setuptools import setup, Extension

TESTS = ["pytest>=3", "mock"]
EXTRAS = {"test": TESTS, ":python_version<'3'": ["enum34"], "docs": ["sphinx"]}
VERSION = read_version()

setuptools.setup(
    name="demo",
    version=VERSION,
    python_requires=">=2.7, !=3.0.*",
    install_requires=["six"],
    extras_require=EXTRAS,
    tests_require=["nose"],
    long_description=open("README").read(),
    test_suite="tests",
    ext_modules=[Extension("demo._speedups", ["src/demo/_speedups.c"])],
    classifiers=sorted(os.listdir(".")),
    **options,
)
"""


def test_setup_py_is_parsed_not_executed() -> None:
    found = detect({"setup.py": SETUP_PY, "tests/test_demo.py": UNITTEST_TEST})
    assert found.backend == "setuptools"
    assert found.fields["python"] == "!=3.0.*,>=2.7"
    assert found.fields["extras"] == ["test"]
    assert found.fields["test_dependencies"] == ["nose"]
    assert found.fields["system_packages"] == ["build-essential"]
    # unittest evidence (test_suite, TestCase) but pytest is declared via the extra
    assert found.fields["test_framework"] == "unittest"
    assert found.fields["test_command"] == [
        "python",
        "-m",
        "unittest",
        "discover",
        "-v",
        "-s",
        "tests",
    ]
    assert "setup.py passes **kwargs to setup(); those values were not read" in found.notes


def test_setup_py_computed_values_are_noted() -> None:
    found = detect(
        {
            "setup.py": "from setuptools import setup\n"
            "REQS = parse('requirements.txt')\n"
            "setup(python_requires=PY, install_requires=REQS, extras_require=get_extras())\n"
        }
    )
    for keyword in ("python_requires", "install_requires", "extras_require"):
        assert f"setup.py: {keyword} is computed when setup.py runs; not read" in found.notes
    assert found.fields["install"] == "editable"


@pytest.mark.parametrize(
    ("source", "note"),
    [
        ("print 'python 2'\n", "setup.py is not valid Python 3 syntax"),
        ("import setuptools\n", "setup.py has no setup() call that could be read"),
    ],
)
def test_setup_py_without_a_readable_call(source: str, note: str) -> None:
    found = detect({"setup.py": source})
    assert any(n.startswith(note) for n in found.notes)
    assert found.fields["install"] == "editable"


def test_requirements_only_repository() -> None:
    found = detect(
        {
            "requirements.txt": "requests==2.0\n",
            "requirements-dev.txt": "pytest\n",
            "dev-requirements.txt": "-r requirements.txt\nflake8\n",
            "requirements-docs.txt": "sphinx\n",
            "docs/requirements.txt": "sphinx\n",
            "requirements/typing.txt": "mypy\n",
            "requirements/ci.txt": "tox\n",
            "requirements.in": "requests\n",
            "tests/test_x.py": "def test_x():\n    pass\n",
        }
    )
    assert found.backend is None
    assert found.fields["install"] == "requirements"
    assert found.fields["requirements_files"] == [
        "requirements.txt",
        "dev-requirements.txt",
        "requirements-dev.txt",
        "requirements/ci.txt",
    ]
    assert "test_dependencies" not in found.fields
    assert "pytest: test files define plain test functions" in found.notes


def test_test_requirements_win_over_dev_requirements() -> None:
    found = detect(
        {
            "setup.py": "from setuptools import setup\nsetup()\n",
            "requirements.txt": "six\n",
            "requirements-dev.txt": "black\n",
            "requirements/tests.txt": "pytest==7.1.2\n",
        }
    )
    assert found.fields["requirements_files"] == ["requirements.txt", "requirements/tests.txt"]
    assert "test_dependencies" not in found.fields


def test_tox_ini() -> None:
    found = detect(
        {
            "setup.py": "from setuptools import setup\nsetup(extras_require={'Tests': ['x']})\n",
            "tox.ini": """
[tox]
envlist = py27,py36

[testenv]
deps =
    -rrequirements/test.txt
    -r ci/missing.txt
    -r ../outside.txt
    -cconstraints.txt
    py27: mock
    {env:EXTRA_DEPS}
    -e .[tests, speedups]
    .
    coverage>=4
extras = docs
commands = python -m unittest discover
setenv =
    PYTHONHASHSEED = 0
    LANG=C.UTF-8
    COVERAGE_FILE = {toxworkdir}/.coverage
    not an assignment
""",
            "requirements/test.txt": "pytest\n",
            "tests/test_a.py": UNITTEST_TEST,
        }
    )
    assert found.sources == ("setup.py", "tox.ini", "requirements/test.txt")
    assert found.fields["requirements_files"] == ["requirements/test.txt"]
    assert found.fields["extras"] == ["docs", "speedups", "tests"]
    assert found.fields["test_dependencies"] == ["coverage>=4"]
    assert found.fields["env"] == {"LANG": "C.UTF-8", "PYTHONHASHSEED": "0"}
    assert found.fields["test_framework"] == "unittest"
    notes = "\n".join(found.notes)
    for expected in (
        "requirements file ci/missing.txt does not exist",
        "requirements file '../outside.txt' (outside the repository)",
        "skipped option line '-cconstraints.txt'",
        "skipped factor-conditional dependency 'py27: mock'",
        "skipped '{env:EXTRA_DEPS}' (uses tox substitution)",
        "skipped setenv line 'COVERAGE_FILE = {toxworkdir}/.coverage'",
        "skipped setenv line 'not an assignment'",
        "unittest: tox.ini commands run unittest",
    ):
        assert expected in notes


def test_tox_pytest_section_and_commands() -> None:
    found = detect({"tox.ini": "[pytest]\naddopts = -q\n"})
    assert "pytest: tox.ini [pytest]" in found.notes
    found = detect({"tox.ini": "[testenv]\ncommands = py.test tests\n"})
    assert "pytest: tox.ini commands run pytest" in found.notes


def test_repository_without_metadata() -> None:
    found = detect({"tox.ini": "[testenv]\nextras = test\n", "mod.py": "x = 1\n"})
    assert found.fields == {
        "install": "none",
        "test_framework": "pytest",
        "test_dependencies": ["pytest"],
    }
    assert "extras ['test'] ignored: there is no installable project" in found.notes
    assert "no packaging metadata or requirements files found" in found.notes
    assert "no test runner evidence; defaulting to pytest" in found.notes
    assert found.recipe().install is InstallMode.NONE


def test_unreadable_files_are_noted_and_skipped() -> None:
    found = detect(
        {
            "pyproject.toml": "[project\nname = 'x'\n",
            "setup.cfg": "no section header\n",
            "tox.ini": b"[testenv]\ndeps = caf\xe9\n",
        }
    )
    notes = "\n".join(found.notes)
    assert "pyproject.toml is not valid TOML" in notes
    assert "setup.cfg could not be parsed (MissingSectionHeaderError)" in notes
    assert "tox.ini is not valid UTF-8" in notes


def test_python_constraint_precedence_and_conflicts() -> None:
    found = detect(
        {
            "pyproject.toml": '[project]\nname = "x"\nrequires-python = "3.x"\n',
            "setup.cfg": "[options]\npython_requires = >=3.7\n",
            "setup.py": "from setuptools import setup\nsetup(python_requires='>=3.6')\n",
        }
    )
    assert found.fields["python"] == ">=3.7"
    notes = "\n".join(found.notes)
    assert "[project] requires-python '3.x' is not a valid specifier; ignored" in notes
    assert "setup.py python_requires '>=3.6' differs from setup.cfg python_requires" in notes


def test_pytest_evidence_sources() -> None:
    assert "pytest: pytest.ini" in detect({"pytest.ini": "[pytest]\n"}).notes
    assert "pytest: conftest.py" in detect({"tests/conftest.py": ""}).notes
    imports = detect({"tests/test_a.py": PYTEST_TEST, "pkg/fast.pyx": ""})
    assert "pytest: test files import pytest" in imports.notes
    assert imports.fields["system_packages"] == ["build-essential"]


def test_unittest_command_uses_top_level_dir_for_test_packages() -> None:
    found = detect({"test/__init__.py": "", "test/test_a.py": UNITTEST_TEST})
    assert found.fields["test_command"][-4:] == ["-s", "test", "-t", "."]
    flat = detect({"test_a.py": UNITTEST_TEST})
    assert flat.fields["test_framework"] == "unittest"
    assert "test_command" not in flat.fields
    assert flat.recipe().test_framework is Framework.UNITTEST


def test_memory_source_rejects_unsafe_paths() -> None:
    with pytest.raises(InvalidPathError):
        MemoryTreeSource({"../x": ""})
    source = MemoryTreeSource({"a.txt": "a"}, commit="0" * 40)
    assert source.commit == "0" * 40
    assert source.read("b.txt") is None


def test_detect_at_reads_the_commit_not_the_work_tree(
    repo_factory: RepoFactory, tmp_path: Path
) -> None:
    repo = repo_factory.create(
        files={
            "pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.6"\n',
            "tests/test_a.py": PYTEST_TEST,
        }
    )
    old = repo.head
    repo.commit("bump", {"pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.8"\n'})
    repo.write({"pyproject.toml": "[project\n"})  # uncommitted garbage in the work tree

    at_old = detect_at(repo.git, old)
    assert at_old.commit == old
    assert at_old.fields["python"] == ">=3.6"
    assert detect_at(repo.git, "HEAD").fields["python"] == ">=3.8"

    bare = Git(tmp_path / "bare.git").clone_from(str(repo.path), bare=True)
    source = GitTreeSource(bare, old)
    assert source.files() == ("pyproject.toml", "tests/test_a.py")
    assert source.read("missing.txt") is None
    assert detect_recipe(source).fields["python"] == ">=3.6"
    with pytest.raises(InvalidPathError):
        source.read("../etc/passwd")


def test_setup_py_literal_evaluation_edge_cases() -> None:
    found = detect(
        {
            "setup.py": """
from setuptools import setup
BASE = ["six"]
PY = ">=3" + ".6"
BAD = {[1]: 2}
MIXED = BASE + "x"
setup(
    python_requires=PY,
    tests_require=BASE + ["pytest"],
    extras_require={**BAD},
    install_requires=MIXED,
)
""",
        }
    )
    assert found.fields["python"] == ">=3.6"
    assert found.fields["test_dependencies"] == ["six", "pytest"]
    assert "setup.py: extras_require is computed when setup.py runs; not read" in found.notes
    assert "setup.py: install_requires is computed when setup.py runs; not read" in found.notes
    unhashable = detect({"setup.py": "setup(extras_require={[1]: ['x']}, tests_require=[f()])"})
    assert "setup.py: extras_require is computed when setup.py runs; not read" in unhashable.notes
    assert "setup.py: tests_require is computed when setup.py runs; not read" in unhashable.notes


def test_odd_values_are_skipped_without_failing() -> None:
    found = detect(
        {
            "pyproject.toml": """
[tool.poetry.dependencies]
python = {version = "^3.8"}

[dependency-groups]
test = ["pytest", {unknown = "x"}, 3]
""",
            "requirements/tests.txt": "--index-url https://example.invalid\nnot valid !!\n",
            "setup.py": "import setuptools\nregistry['setup'](name='x')\nsetuptools.setup()\n",
        }
    )
    assert found.fields["test_dependencies"] == ["pytest"]
    assert found.fields["requirements_files"] == ["requirements/tests.txt"]
    assert any("python constraint {'version': '^3.8'} was not translated" in n for n in found.notes)
