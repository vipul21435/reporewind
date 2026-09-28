from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fakes import FakeRunner, failed, ok
from reporewind.errors import PinError
from reporewind.pinning.lock import (
    DEFAULT_PLATFORM,
    LockRequest,
    compile_lock,
    format_timestamp,
    lock_header,
    pinned_packages,
    uv_environment,
)
from reporewind.recipes.source import MemoryTreeSource

WHEN = datetime(2021, 6, 1, 17, 30, 5, tzinfo=timezone(timedelta(hours=2)))
LOCK = """\
click==8.0.1 \\
    --hash=sha256:aaaa
    # via -r requirements.in
colorama==0.4.4 ; sys_platform == 'win32'
    # via click
pytest-cov==2.12.1
    # via -r repo/requirements/tests.txt
uvicorn[standard]==0.14.0
"""


def request(**changes: object) -> LockRequest:
    data: dict[str, object] = {
        "python_version": "3.9",
        "exclude_newer": WHEN,
        "requirements": ("click>=8", "pytest"),
    }
    data.update(changes)
    return LockRequest.model_validate(data)


def test_format_timestamp_is_utc_seconds() -> None:
    assert format_timestamp(WHEN) == "2021-06-01T15:30:05Z"
    assert format_timestamp(datetime(2020, 1, 1, 0, 0, 0, 999, tzinfo=UTC)) == (
        "2020-01-01T00:00:00Z"
    )


def test_pinned_packages_reads_names_and_versions() -> None:
    assert pinned_packages(LOCK) == (
        "click==8.0.1",
        "colorama==0.4.4",
        "pytest-cov==2.12.1",
        "uvicorn==0.14.0",
    )


def test_uv_environment_drops_uv_settings_but_keeps_the_cache() -> None:
    env = {"PATH": "/bin", "UV_INDEX_URL": "https://mirror", "UV_CACHE_DIR": "/c", "HOME": "/h"}
    assert uv_environment(env) == {"PATH": "/bin", "UV_CACHE_DIR": "/c", "HOME": "/h"}
    assert "PATH" in uv_environment()


def test_compile_runs_uv_with_the_date_python_and_platform() -> None:
    runner = FakeRunner({"uv pip": ok(LOCK, "Resolved 4 packages in 12ms\n")})
    tree = MemoryTreeSource(
        {
            "requirements/tests.txt": "-r base.txt\npytest-cov  # coverage\n-e .\n",
            "requirements/base.txt": "--requirement=../constraints.txt\nclick\n",
            "constraints.txt": "click<9\n",
        }
    )
    result = compile_lock(
        tree,
        request(requirements_files=("requirements/tests.txt",)),
        runner,
        env={"PATH": "/usr/bin", "UV_INDEX_URL": "x"},
    )
    (call,) = runner.calls
    assert call.argv == (
        "uv",
        "pip",
        "compile",
        "--no-config",
        "--no-header",
        "--python-version",
        "3.9",
        "--python-platform",
        DEFAULT_PLATFORM,
        "--exclude-newer",
        "2021-06-01T15:30:05Z",
        "--generate-hashes",
        "requirements.in",
        "repo/requirements/tests.txt",
    )
    assert call.env == {"PATH": "/usr/bin"}
    assert call.inputs == {
        "requirements.in": "click>=8\npytest\n",
        "repo/requirements/tests.txt": "-r base.txt\npytest-cov  # coverage\n",
        "repo/requirements/base.txt": "--requirement=../constraints.txt\nclick\n",
        "repo/constraints.txt": "click<9\n",
    }
    assert result.argv == call.argv
    assert result.text == lock_header(request()) + LOCK
    assert result.text.startswith(
        "# Locked by reporewind for Python 3.9 on x86_64-unknown-linux-gnu\n"
        "# from packages published up to 2021-06-01T15:30:05Z (uv pip compile --exclude-newer).\n"
    )
    assert result.sha256.startswith("sha256:")
    assert len(result.sha256) == 71
    assert result.packages[0] == "click==8.0.1"
    assert result.notes == (
        "requirements/tests.txt: dropped '-e .' (the project is installed separately)",
    )
    assert call.cwd is not None
    assert not Path(call.cwd).exists()


def test_offline_and_no_hashes_flags() -> None:
    runner = FakeRunner({"uv pip": ok(LOCK)})
    compile_lock(
        MemoryTreeSource({}), request(generate_hashes=False, offline=True, platform="linux"), runner
    )
    argv = runner.calls[0].argv
    assert "--generate-hashes" not in argv
    assert "--offline" in argv
    assert argv[argv.index("--python-platform") + 1] == "linux"


def test_nothing_to_lock_skips_uv() -> None:
    runner = FakeRunner()
    result = compile_lock(MemoryTreeSource({}), request(requirements=()), runner)
    assert runner.calls == []
    assert result.packages == ()
    assert result.text == lock_header(request())
    assert result.notes == ("nothing to lock: no requirements and no requirements files",)


def test_uv_failure_is_a_pin_error_with_its_stderr() -> None:
    runner = FakeRunner(
        {"uv pip": failed("  x No solution found when resolving dependencies:\n  pkg>=99\n", 1)}
    )
    with pytest.raises(PinError, match=r"uv pip compile failed \(exit 1\):\n.*No solution found"):
        compile_lock(MemoryTreeSource({}), request(), runner)


def test_includes_outside_the_repository_are_dropped() -> None:
    runner = FakeRunner({"uv pip": ok(LOCK)})
    tree = MemoryTreeSource({"requirements.txt": "-r ../../etc/passwd\n-c/abs.txt\nsix\n"})
    result = compile_lock(tree, request(requirements_files=("requirements.txt",)), runner)
    assert runner.calls[0].inputs["repo/requirements.txt"] == "six\n"
    assert result.notes == (
        "requirements.txt: dropped '-r ../../etc/passwd' (outside the repository)",
        "requirements.txt: dropped '-c/abs.txt' (outside the repository)",
    )


def test_missing_requirements_file_is_an_error() -> None:
    tree = MemoryTreeSource({"requirements.txt": "-r missing.txt\n"})
    with pytest.raises(PinError, match=r"requirements file missing\.txt does not exist"):
        compile_lock(tree, request(requirements_files=("requirements.txt",)), FakeRunner())


def test_include_cycles_are_staged_once() -> None:
    runner = FakeRunner({"uv pip": ok(LOCK)})
    tree = MemoryTreeSource({"a.txt": "-r b.txt\nsix\n", "b.txt": "-r a.txt\nattrs\n"})
    compile_lock(tree, request(requirements_files=("a.txt", "b.txt")), runner)
    assert runner.calls[0].argv[-2:] == ("repo/a.txt", "repo/b.txt")
    assert set(runner.calls[0].inputs) == {"requirements.in", "repo/a.txt", "repo/b.txt"}


def test_deep_include_chains_are_rejected() -> None:
    files = {f"r{i}.txt": f"-r r{i + 1}.txt\n" for i in range(12)}
    files["r12.txt"] = "six\n"
    with pytest.raises(PinError, match="nest deeper than 10"):
        compile_lock(MemoryTreeSource(files), request(requirements_files=("r0.txt",)), FakeRunner())


@pytest.mark.e2e
def test_real_uv_locks_as_of_the_commit_date() -> None:
    from reporewind.proc import SubprocessRunner

    result = compile_lock(
        MemoryTreeSource({}),
        request(
            requirements=("pytest",),
            exclude_newer=datetime(2021, 1, 1, tzinfo=UTC),
        ),
        SubprocessRunner(),
    )
    assert "pytest==6.2.1" in result.packages
