from reporewind.pinning.metadata import ProjectMetadata, read_metadata
from reporewind.recipes.source import MemoryTreeSource


def test_pep621_classifiers_dependencies_and_extras() -> None:
    tree = MemoryTreeSource(
        {
            "pyproject.toml": """
[project]
name = "demo"
classifiers = ["Programming Language :: Python :: 3.9", "License :: OSI Approved"]
dependencies = ["click >= 8", "attrs", "not a requirement !"]

[project.optional-dependencies]
Fast_IO = ["orjson>=3"]
tests = ["pytest"]
"""
        }
    )
    meta = read_metadata(tree)
    assert meta.classifiers == (
        "Programming Language :: Python :: 3.9",
        "License :: OSI Approved",
    )
    assert meta.dependencies == ("click>=8", "attrs")
    assert meta.extras == {"fast-io": ("orjson>=3",), "tests": ("pytest",)}
    assert meta.sources == ("pyproject.toml",)
    assert meta.notes == (
        "pyproject.toml dependencies: skipped 'not a requirement !' (not a PEP 508 requirement)",
    )
    assert meta.requirements(["Fast.IO", "missing"]) == ("click>=8", "attrs", "orjson>=3")


def test_dynamic_fields_and_bad_toml_are_noted() -> None:
    dynamic = read_metadata(
        MemoryTreeSource(
            {
                "pyproject.toml": '[project]\nname = "x"\n'
                'dynamic = ["dependencies", "optional-dependencies"]\n'
            }
        )
    )
    assert dynamic.dependencies == ()
    assert dynamic.notes == (
        "pyproject.toml: dependencies are dynamic (computed at build time)",
        "pyproject.toml: optional-dependencies are dynamic (computed at build time)",
    )
    broken = read_metadata(MemoryTreeSource({"pyproject.toml": "[project\n"}))
    assert broken.notes == ("pyproject.toml is not valid TOML; it was skipped",)


def test_poetry_dependencies_and_extras() -> None:
    tree = MemoryTreeSource(
        {
            "pyproject.toml": """
[tool.poetry]
name = "demo"
classifiers = ["Programming Language :: Python :: 3.8"]

[tool.poetry.dependencies]
python = "^3.8"
requests = "^2.25"
ujson = { version = "^4.0", optional = true }
local = { path = "../local" }

[tool.poetry.extras]
speed = ["ujson", "undeclared"]
"""
        }
    )
    meta = read_metadata(tree)
    assert meta.classifiers == ("Programming Language :: Python :: 3.8",)
    # ujson is optional = true: Poetry installs it only through the speed extra.
    assert meta.name == "demo"
    assert meta.dependencies == ("requests<3,>=2.25",)
    assert meta.requirements(()) == ("requests<3,>=2.25",)
    assert meta.requirements(["speed"]) == ("requests<3,>=2.25", "ujson<5,>=4.0")
    assert meta.extras == {"speed": ("ujson<5,>=4.0",)}
    assert meta.notes == (
        "pyproject.toml: poetry dependency skipped: local is installed from a path source,"
        " not an index",
        "pyproject.toml: poetry extra speed names undeclared 'undeclared'",
    )


def test_setup_cfg_metadata() -> None:
    tree = MemoryTreeSource(
        {
            "setup.cfg": """
[metadata]
name = demo
classifiers =
    Programming Language :: Python :: 3.7
    Programming Language :: Python :: 3.8

[options]
install_requires =
    six>=1.10
    # a comment
    requests

[options.extras_require]
yaml = pyyaml>=5
"""
        }
    )
    meta = read_metadata(tree)
    assert meta.classifiers == (
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
    )
    assert meta.dependencies == ("six>=1.10", "requests")
    assert meta.extras == {"yaml": ("pyyaml>=5",)}


def test_setup_cfg_file_directive_and_parse_error_are_noted() -> None:
    meta = read_metadata(
        MemoryTreeSource({"setup.cfg": "[options]\ninstall_requires = file: requirements.txt\n"})
    )
    assert meta.dependencies == ()
    assert meta.notes == ("setup.cfg: install_requires reads a file at build time; not read",)
    broken = read_metadata(MemoryTreeSource({"setup.cfg": "no section header\n"}))
    assert broken.notes == ("setup.cfg could not be parsed; it was skipped",)


def test_setup_py_literals_are_read_without_running_it() -> None:
    tree = MemoryTreeSource(
        {
            "setup.py": """
raise SystemExit("setup.py must never run during pinning")
from setuptools import setup
BASE = ["six"]
setup(
    name="demo",
    classifiers=["Programming Language :: Python :: 3.7"],
    install_requires=BASE + ["requests>=2"],
    extras_require={"yaml": ["pyyaml"], ":python_version<'3'": ["enum34"]},
    version=compute_version(),
)
"""
        }
    )
    meta = read_metadata(tree)
    assert meta.classifiers == ("Programming Language :: Python :: 3.7",)
    assert meta.dependencies == ("six", "requests>=2")
    assert meta.extras == {"yaml": ("pyyaml",)}
    assert meta.notes == (
        "setup.py extras_require: skipped conditional extra \":python_version<'3'\"",
    )


def test_setup_py_computed_values_and_syntax_errors_are_noted() -> None:
    computed = read_metadata(
        MemoryTreeSource(
            {"setup.py": "from setuptools import setup\nsetup(install_requires=read_reqs())\n"}
        )
    )
    assert computed.notes == (
        "setup.py: install_requires is computed when setup.py runs; not read",
    )
    py2 = read_metadata(MemoryTreeSource({"setup.py": "print 'hello'\n"}))
    assert py2.notes == ("setup.py is not valid Python 3 syntax; its arguments were not read",)
    no_call = read_metadata(MemoryTreeSource({"setup.py": "import setuptools\n"}))
    assert no_call == ProjectMetadata(sources=("setup.py",))


def test_first_file_with_classifiers_wins_and_dependencies_merge() -> None:
    tree = MemoryTreeSource(
        {
            "pyproject.toml": '[project]\nname = "x"\ndependencies = ["a"]\n'
            'classifiers = ["Programming Language :: Python :: 3.11"]\n',
            "setup.cfg": "[metadata]\nclassifiers = Programming Language :: Python :: 3.7\n"
            "[options]\ninstall_requires =\n    a\n    b\n",
        }
    )
    meta = read_metadata(tree)
    assert meta.classifiers == ("Programming Language :: Python :: 3.11",)
    assert meta.dependencies == ("a", "b")
    assert meta.sources == ("pyproject.toml", "setup.cfg")


def test_empty_tree_has_no_metadata() -> None:
    assert read_metadata(MemoryTreeSource({"README.md": "hi"})) == ProjectMetadata()


def test_self_referencing_extras_expand_to_this_commits_extras() -> None:
    tree = MemoryTreeSource(
        {
            "pyproject.toml": """
[project]
name = "Attrs"
dependencies = ["importlib_metadata; python_version < '3.8'"]

[project.optional-dependencies]
tests-no-zope = ["mypy>=1.1.1", "pytest"]
tests = ["attrs[tests-no-zope]", "zope.interface"]
cov = ["attrs[tests]; python_version >= '3.8'", "coverage[toml]>=5.3"]
loop = ["attrs[loop]", "six"]
"""
        }
    )
    meta = read_metadata(tree)
    assert meta.name == "Attrs"
    assert meta.requirements(["tests"]) == (
        'importlib_metadata; python_version < "3.8"',
        "mypy>=1.1.1",
        "pytest",
        "zope.interface",
    )
    assert meta.requirements(["cov"]) == (
        'importlib_metadata; python_version < "3.8"',
        'mypy>=1.1.1; python_version >= "3.8"',
        'pytest; python_version >= "3.8"',
        'zope.interface; python_version >= "3.8"',
        "coverage[toml]>=5.3",
    )
    # A cycle terminates, and dependency-group style lists expand the same way.
    assert meta.requirements(["loop"])[1:] == ("six",)
    assert meta.expand(["attrs[tests-no-zope]", "hypothesis"]) == (
        "mypy>=1.1.1",
        "pytest",
        "hypothesis",
    )


def test_project_name_from_setup_cfg_and_setup_py() -> None:
    cfg = read_metadata(MemoryTreeSource({"setup.cfg": "[metadata]\nname = demo\n"}))
    assert cfg.name == "demo"
    literal = read_metadata(MemoryTreeSource({"setup.py": "setup(name='lit')\n"}))
    assert literal.name == "lit"
    computed = read_metadata(MemoryTreeSource({"setup.py": "setup(name=NAME + 'x')\n"}))
    assert computed.name is None
    assert computed.notes == ()
    assert read_metadata(MemoryTreeSource({})).expand(["attrs[x]"]) == ("attrs[x]",)


def test_poetry_multiple_constraint_optional_dependency_is_skipped() -> None:
    tree = MemoryTreeSource(
        {
            "pyproject.toml": """
[tool.poetry]
name = "demo"

[tool.poetry.dependencies]
six = "^1.15"
numpy = [
  { version = "^1.20", python = ">=3.8", optional = true },
  { version = "^1.19", python = "<3.8", optional = true },
]
pyyaml = { version = "^5.4", optional = true }

[tool.poetry.extras]
yaml = ["pyyaml"]
"""
        }
    )
    meta = read_metadata(tree)
    assert meta.dependencies == ("six<2,>=1.15",)
    assert meta.requirements(["yaml"]) == ("six<2,>=1.15", "pyyaml<6,>=5.4")
