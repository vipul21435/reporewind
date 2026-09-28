from datetime import UTC, datetime

from fakes import FakeRunner, ok
from reporewind.pinning.lock import LockRequest, compile_lock
from reporewind.recipes.source import GitTreeSource
from reporewind.testing import RepoFactory


def test_symlinked_requirements_files_are_read_through_the_link(
    repo_factory: RepoFactory,
) -> None:
    repo = repo_factory.create(
        "links", {"requirements/base.txt": "six\n", "tests/test_x.py": "def test(): pass\n"}
    )
    (repo.path / "requirements.txt").symlink_to("requirements/base.txt")
    (repo.path / "requirements" / "dev.txt").symlink_to("../requirements.txt")
    (repo.path / "escape.txt").symlink_to("../outside.txt")
    (repo.path / "absolute.txt").symlink_to("/etc/hosts")
    (repo.path / "dangling.txt").symlink_to("missing.txt")
    (repo.path / "loop-a.txt").symlink_to("loop-b.txt")
    (repo.path / "loop-b.txt").symlink_to("loop-a.txt")
    repo.commit("add links")
    tree = GitTreeSource(repo.git, "HEAD")

    assert repo.git.symlinks("HEAD")["requirements.txt"] == "requirements/base.txt"
    assert tree.read("requirements.txt") == b"six\n"
    assert tree.read("requirements/dev.txt") == b"six\n"
    assert tree.read("requirements/base.txt") == b"six\n"
    for broken in ("escape.txt", "absolute.txt", "dangling.txt", "loop-a.txt", "nope.txt"):
        assert tree.read(broken) is None, broken

    runner = FakeRunner({"uv pip": ok("six==1.16.0\n")})
    request = LockRequest(
        python_version="3.11",
        exclude_newer=datetime(2023, 6, 1, tzinfo=UTC),
        requirements_files=("requirements.txt",),
        generate_hashes=False,
    )
    result = compile_lock(tree, request, runner)
    assert runner.calls[0].inputs["repo/requirements.txt"] == "six\n"
    assert result.packages == ("six==1.16.0",)
