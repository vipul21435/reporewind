"""Build the demo repository and write it as a git fast-import stream.

The demo repository (``slugkit``, a tiny slug helper) is generated with
:class:`reporewind.testing.RepoFactory`, whose fixed identity and clock make
every commit SHA identical on every machine. ``git fast-export`` turns it into
a plain-text stream (``demo/slugkit.fi``) that ``demo/run.sh`` imports, so the
demo needs no network and the bundled data is reviewable as text.

Regenerate with ``uv run python demo/make_sample.py``; a test checks that the
committed stream matches this script byte for byte.

History (tags mark the commits the demo resolves)::

    initial -> docs -> fix-separators -> docs --------> merge-unicode -> refactor-only
                                           \\-> unicode fix ---/
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from reporewind.gitops import Git
from reporewind.testing import RepoFactory

STREAM = Path(__file__).with_name("slugkit.fi")

PYPROJECT = """\
[project]
name = "slugkit"
version = "0.1.0"
requires-python = ">=3.9"
"""

INIT = """\
from slugkit.core import slugify

__all__ = ["slugify"]
"""

CORE_BUGGY = '''\
"""Turn titles into URL slugs."""

import re

_UNSAFE = re.compile(r"[^a-z0-9]")


def slugify(title, sep="-"):
    """Lowercase ``title`` and replace every unsafe character with ``sep``."""
    return _UNSAFE.sub(sep, title.strip().lower())
'''

CORE_RUNS_FIXED = '''\
"""Turn titles into URL slugs."""

import re

_UNSAFE = re.compile(r"[^a-z0-9]+")


def slugify(title, sep="-"):
    """Lowercase ``title``; collapse each run of unsafe characters into one ``sep``."""
    return _UNSAFE.sub(sep, title.lower()).strip(sep)
'''

CORE_UNICODE_FIXED = '''\
"""Turn titles into URL slugs."""

import re
import unicodedata

_UNSAFE = re.compile(r"[^a-z0-9]+")


def slugify(title, sep="-"):
    """Lowercase ``title``; collapse each run of unsafe characters into one ``sep``."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return _UNSAFE.sub(sep, ascii_title.lower()).strip(sep)
'''

CORE_REFACTORED = '''\
"""Turn titles into URL slugs."""

import re
import unicodedata

_UNSAFE_RUN = re.compile(r"[^a-z0-9]+")


def _to_ascii(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def slugify(title, sep="-"):
    """Lowercase ``title``; collapse each run of unsafe characters into one ``sep``."""
    return _UNSAFE_RUN.sub(sep, _to_ascii(title).lower()).strip(sep)
'''

TEST_CORE = """\
import unittest

from slugkit import slugify


class SlugifyTest(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(slugify("Hello"), "hello")
"""

TEST_CORE_RUNS = (
    TEST_CORE
    + """
    def test_collapses_runs_of_separators(self):
        self.assertEqual(slugify("  Hello,  World! "), "hello-world")
"""
)

TEST_UNICODE = """\
import unittest

from slugkit import slugify


class UnicodeTest(unittest.TestCase):
    def test_transliterates_accents(self):
        self.assertEqual(slugify("Cr\\u00e8me br\\u00fbl\\u00e9e"), "creme-brulee")
"""


def build(root: Path) -> Path:
    """Create the slugkit history under ``root`` and return the repository path."""
    repo = RepoFactory(root).create(
        "slugkit",
        files={
            "pyproject.toml": PYPROJECT,
            "README.md": "# slugkit\n",
            "src/slugkit/__init__.py": INIT,
            "src/slugkit/core.py": CORE_BUGGY,
            "tests/test_core.py": TEST_CORE,
        },
    )
    repo.commit(
        "docs: add usage",
        {"README.md": '# slugkit\n\n    slugify("Hello World")  # hello-world\n'},
    )
    repo.commit(
        "fix: collapse runs of separators in slugify",
        {
            "src/slugkit/core.py": CORE_RUNS_FIXED,
            "tests/test_core.py": TEST_CORE_RUNS,
            "CHANGES.md": "- slugify collapses runs of separators and trims both ends\n",
        },
    )
    repo.tag("fix-separators")
    repo.branch("unicode-slugs")
    repo.commit("docs: list supported Python versions", {"README.md": "# slugkit\n\nPython 3.9+\n"})
    repo.switch("unicode-slugs")
    repo.commit(
        "fix: transliterate accented letters instead of dropping them",
        {"src/slugkit/core.py": CORE_UNICODE_FIXED, "tests/test_unicode.py": TEST_UNICODE},
    )
    repo.switch("main")
    repo.merge("unicode-slugs", "Merge pull request #7 from example/unicode-slugs")
    repo.tag("merge-unicode")
    repo.commit("refactor: name the ASCII folding step", {"src/slugkit/core.py": CORE_REFACTORED})
    repo.tag("refactor-only")
    return repo.path


def export_stream(repo_dir: Path) -> bytes:
    """Return the full history of ``repo_dir`` as a ``git fast-export`` stream."""
    return Git(repo_dir).run("fast-export", "--all").stdout


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        STREAM.write_bytes(export_stream(build(Path(tmp))))
    print(f"wrote {STREAM}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
