import json
import multiprocessing
import os
import signal
from datetime import UTC, datetime
from pathlib import Path

import pytest

from buildkit import LOCK, FileRegistryBuilder, pin_for
from fakes import FakeRunner
from reporewind.build import (
    BuildCache,
    BuildLock,
    BuildLockTimeoutError,
    DockerBuilder,
    ensure_image,
    environment_key,
    export_source,
    image_tag,
    render_dockerfile,
)
from reporewind.errors import BuildError, CommandError
from reporewind.proc import CommandResult
from reporewind.recipes.model import Recipe
from reporewind.testing import RepoFactory

RECIPE = Recipe()
PIN = pin_for(RECIPE)
FIXED = datetime(2026, 9, 29, tzinfo=UTC)


def write_source(dest: Path) -> None:
    dest.mkdir(parents=True)
    (dest / "setup.py").write_text("from setuptools import setup\nsetup(name='calc')\n")


def build_once(root: str) -> tuple[str, str]:
    """One builder process: render, then ensure the image through the shared cache."""
    base = Path(root)
    outcome = ensure_image(
        PIN,
        render_dockerfile(RECIPE, PIN, LOCK),
        LOCK,
        builder=FileRegistryBuilder(base / "registry"),
        cache=BuildCache(base / "cache", lock_timeout=60),
        write_source=write_source,
    )
    return outcome.tag, outcome.status


def test_key_and_tag_depend_on_every_input() -> None:
    key = environment_key(PIN, LOCK)
    assert key.startswith("sha256:")
    assert len(key) == 71
    assert environment_key(pin_for(RECIPE), LOCK) == key
    other_lock = LOCK.replace("6.2.4", "6.2.5")
    assert environment_key(pin_for(RECIPE, other_lock), other_lock) != key
    assert environment_key(pin_for(RECIPE, digest="sha256:" + "cd" * 32), LOCK) != key
    changed = Recipe(env={"A": "1"})
    assert environment_key(pin_for(changed), LOCK) != key
    assert image_tag(PIN.repo, key) == f"reporewind/example__calc:env-{key[7:23]}"


def test_second_build_is_a_cache_hit(tmp_path: Path) -> None:
    builder = FileRegistryBuilder(tmp_path / "registry", delay=0)
    cache = BuildCache(tmp_path / "cache")
    dockerfile = render_dockerfile(RECIPE, PIN, LOCK)
    kwargs = {"builder": builder, "cache": cache, "write_source": write_source}
    first = ensure_image(PIN, dockerfile, LOCK, now=lambda: FIXED, **kwargs)  # type: ignore[arg-type]
    second = ensure_image(PIN, dockerfile, LOCK, **kwargs)  # type: ignore[arg-type]
    assert (first.status, second.status) == ("built", "cached")
    assert first.tag == second.tag
    assert first.image_id == second.image_id
    assert builder.builds() == [
        {"tag": first.tag, "files": ["Dockerfile", "requirements.lock", "src/setup.py"]}
    ]
    index = json.loads(cache.index_path.read_text())
    assert index["images"][first.key]["built_at"] == "2026-09-29T00:00:00Z"
    assert index["images"][first.key]["commit"] == PIN.commit
    # The index outlives the image: a pruned image is rebuilt, not trusted.
    for image in (tmp_path / "registry" / "images").iterdir():
        image.unlink()
    third = ensure_image(PIN, dockerfile, LOCK, **kwargs)  # type: ignore[arg-type]
    assert third.status == "built"
    assert len(builder.builds()) == 2
    assert not list(cache.root.glob("context-*"))


def test_concurrent_builders_produce_exactly_one_build(tmp_path: Path) -> None:
    workers = 6
    context = multiprocessing.get_context("spawn")
    with context.Pool(workers) as pool:
        results = pool.map(build_once, [str(tmp_path)] * workers)
    builds = FileRegistryBuilder(tmp_path / "registry").builds()
    assert len(builds) == 1
    assert {tag for tag, _ in results} == {builds[0]["tag"]}
    assert sorted(status for _, status in results) == ["built"] + ["cached"] * (workers - 1)
    index = json.loads((tmp_path / "cache" / "index.json").read_text())
    assert len(index["images"]) == 1


def test_corrupt_index_is_a_build_error(tmp_path: Path) -> None:
    cache = BuildCache(tmp_path)
    cache.index_path.write_text("{not json")
    with pytest.raises(BuildError, match="not a valid build-cache index"):
        cache.get("sha256:x")


def test_lock_times_out_and_names_the_holder(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "env.lock"
    with BuildLock(path) as held:
        assert held.held
        with pytest.raises(BuildLockTimeoutError, match=rf"held by pid {os.getpid()} on "):
            BuildLock(path, timeout=0.2).acquire()
        with pytest.raises(BuildError, match="already held by this lock object"):
            held.acquire()
    assert path.read_text() == ""  # released cleanly
    with BuildLock(path, timeout=0.2) as again:
        assert not again.recovered_stale


def _hold_and_die(path: str, ready: str) -> None:
    lock = BuildLock(Path(path))
    lock.acquire()
    Path(ready).touch()
    os.kill(os.getpid(), signal.SIGKILL)


def test_lock_left_by_a_killed_holder_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "env.lock"
    process = multiprocessing.get_context("spawn").Process(
        target=_hold_and_die, args=(str(path), str(tmp_path / "ready"))
    )
    process.start()
    process.join(30)
    assert process.exitcode == -signal.SIGKILL
    assert json.loads(path.read_text())["pid"] == process.pid  # the stale holder record
    with BuildLock(path, timeout=1) as lock:
        assert lock.recovered_stale
        assert json.loads(path.read_text())["pid"] == os.getpid()


def test_docker_builder_runs_build_and_inspect(tmp_path: Path) -> None:
    ok = CommandResult(argv=(), returncode=0, stdout=b"sha256:abc\n", stderr=b"")
    runner = FakeRunner({"docker build": ok, "docker image": ok})
    builder = DockerBuilder(runner)
    assert builder.build(tmp_path, "reporewind/x:env-1") == "sha256:abc"
    assert runner.calls[0].argv == (
        "docker", "build", "--label", "project=reporewind", "--tag", "reporewind/x:env-1",
        str(tmp_path),
    )  # fmt: skip
    missing = CommandResult(argv=(), returncode=1, stdout=b"", stderr=b"No such image")
    assert DockerBuilder(FakeRunner({"docker image": missing})).image_id("t") is None
    failed = DockerBuilder(FakeRunner({"docker build": missing}))
    with pytest.raises(CommandError):
        failed.build(tmp_path, "t")
    gone = DockerBuilder(FakeRunner({"docker build": ok, "docker image": missing}))
    with pytest.raises(BuildError, match="not in the local image store"):
        gone.build(tmp_path, "t")


def test_export_source_writes_the_tree_without_git(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create("src")
    sha = repo.commit("add files", {"pkg/a.py": "A = 1\n", "README": "x\n"})
    dest = repo_factory.root / "out"
    export_source(repo.git, sha, dest)
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        "README",
        "pkg/a.py",
    ]
    assert (dest / "pkg" / "a.py").read_text() == "A = 1\n"
