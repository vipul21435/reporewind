import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from reporewind.errors import (
    ConfigError,
    GitError,
    InvalidPathError,
    InvalidRevisionError,
    PatchApplyError,
    RevisionNotFoundError,
    ToolNotFoundError,
)
from reporewind.gitops import ALLOWED_PROTOCOLS, Git, check_revision, hermetic_env
from reporewind.proc import CommandResult
from reporewind.testing import FIXTURE_EPOCH, FIXTURE_STEP, FixtureRepo, RepoFactory


@pytest.fixture
def history(repo_factory: RepoFactory) -> tuple[FixtureRepo, list[str]]:
    """Three linear commits touching src/, tests/ and a binary file."""
    repo = repo_factory.create("upstream")
    shas = [
        repo.commit("chore: initial", {"src/calc.py": "def add(a, b):\n    return a - b\n"}),
        repo.commit(
            "fix: add really adds",
            {
                "src/calc.py": "def add(a, b):\n    return a + b\n",
                "tests/test_calc.py": "from src.calc import add\n\n\ndef test_add():\n"
                "    assert add(2, 2) == 4\n",
            },
        ),
        repo.commit("docs: notes", {"NOTES.md": "notes\n", "logo.bin": b"\x89PNG\x00\x01"}),
    ]
    return repo, shas


# --- environment and argument hygiene ------------------------------------------


def test_hermetic_env_scrubs_redirects_and_injected_config() -> None:
    base = {
        "PATH": "/bin",
        "HOME": "/home/u",
        "GIT_DIR": "/elsewhere/.git",
        "GIT_INDEX_FILE": "/elsewhere/index",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "diff.noprefix",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_CONFIG_GLOBAL": "/home/u/.gitconfig",
    }
    env = hermetic_env(base, extra={"GIT_AUTHOR_NAME": "x"})
    assert env["PATH"] == "/bin"
    assert env["HOME"] == "/home/u"
    for key in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0"):
        assert key not in env
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == ALLOWED_PROTOCOLS
    assert env["GIT_AUTHOR_NAME"] == "x"


@pytest.mark.parametrize("rev", ["", "-x", "--output=/tmp/pwn", "a b", "a\nb", "a\x00b"])
def test_check_revision_rejects_option_like_or_malformed_input(rev: str) -> None:
    with pytest.raises(InvalidRevisionError):
        check_revision(rev)


@pytest.mark.parametrize("rev", ["HEAD", "HEAD~1", "main", "v1.2^{commit}", "a" * 40])
def test_check_revision_accepts_ordinary_revisions(rev: str) -> None:
    assert check_revision(rev) == rev


def test_user_config_and_inherited_git_env_do_not_leak(
    history: tuple[FixtureRepo, list[str]],
    repo_factory: RepoFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, shas = history
    other = repo_factory.create("other", files={"x": "1"})
    user_config = tmp_path / "gitconfig"
    user_config.write_text("[diff]\n\tnoprefix = true\n\trenames = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user_config))
    monkeypatch.setenv("GIT_DIR", str(other.path / ".git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.mnemonicPrefix")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    git = Git(repo.path)
    assert git.rev_parse("HEAD") == shas[-1]
    assert git.diff(shas[0], shas[1]).startswith("diff --git a/src/calc.py b/src/calc.py\n")


def test_commands_get_repo_cwd_hermetic_env_and_timeout(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class Recorder:
        def run(
            self,
            argv: Sequence[str],
            *,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            stdin: bytes | None = None,
            timeout: float | None = None,
        ) -> CommandResult:
            calls.append({"argv": tuple(argv), "cwd": cwd, "env": env, "timeout": timeout})
            return CommandResult(tuple(argv), 128, b"", b"fatal: boom\n")

    git = Git(tmp_path / "r", runner=Recorder(), executable="git-x", timeout=7.0)
    result = git.run("status", check=False)
    assert result.returncode == 128
    with pytest.raises(PatchApplyError, match="fatal: boom"):
        git.run("apply", error=PatchApplyError)
    call = calls[0]
    assert call["argv"] == ("git-x", "status")
    assert call["cwd"] == git.repo_dir
    assert call["timeout"] == 7.0
    env = call["env"]
    assert isinstance(env, dict)
    assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path.resolve())
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert repr(git) == f"Git({str(git.repo_dir)!r})"


def test_missing_git_binary_is_a_tool_error(history: tuple[FixtureRepo, list[str]]) -> None:
    repo, _ = history
    with pytest.raises(ToolNotFoundError):
        Git(repo.path, executable="reporewind-no-such-git").rev_parse("HEAD")


# --- repository discovery ----------------------------------------------------------


def test_is_repo_ignores_enclosing_repositories(history: tuple[FixtureRepo, list[str]]) -> None:
    repo, _ = history
    nested = repo.path / "plain-dir"
    nested.mkdir()
    assert Git(repo.path).is_repo()
    assert not Git(nested).is_repo()
    assert not Git(repo.path / "missing").is_repo()
    with pytest.raises(GitError) as exc:
        Git(nested).rev_parse("HEAD")
    assert not isinstance(exc.value, RevisionNotFoundError)


# --- queries -----------------------------------------------------------------------


def test_rev_parse_resolves_names_to_full_shas(history: tuple[FixtureRepo, list[str]]) -> None:
    repo, shas = history
    git = repo.git
    assert git.rev_parse("HEAD") == shas[2]
    assert git.rev_parse("HEAD~2") == shas[0]
    assert git.rev_parse("main") == shas[2]
    assert git.rev_parse(shas[1][:10]) == shas[1]
    assert git.has_commit(shas[0])
    assert not git.has_commit("f" * 40)
    with pytest.raises(RevisionNotFoundError) as exc:
        git.rev_parse("no-such-branch")
    assert exc.value.revision == "no-such-branch"


def test_rev_parse_rejects_blob_ids(history: tuple[FixtureRepo, list[str]]) -> None:
    repo, _ = history
    blob = repo.git.run("rev-parse", "HEAD:src/calc.py").stdout_text.strip()
    with pytest.raises(RevisionNotFoundError):
        repo.git.rev_parse(blob)


def test_commit_info_parents_and_dates(history: tuple[FixtureRepo, list[str]]) -> None:
    repo, shas = history
    info = repo.git.commit_info("HEAD~1")
    assert info.sha == shas[1]
    assert info.parents == (shas[0],)
    assert info.subject == "fix: add really adds"
    assert info.author_date == FIXTURE_EPOCH + FIXTURE_STEP
    assert repo.git.parents(shas[0]) == ()
    assert repo.git.commit_info(shas[0]).is_root
    assert repo.git.commit_date("HEAD") == FIXTURE_EPOCH + 2 * FIXTURE_STEP


def test_commit_subject_may_contain_any_printable_text(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create()
    repo.commit("fix: handle %x00 and | pipes; $(no) `shell`", {"a": "1"})
    assert repo.git.commit_info("HEAD").subject == "fix: handle %x00 and | pipes; $(no) `shell`"


def test_diff_is_reapplicable_with_full_index_and_ab_prefixes(
    history: tuple[FixtureRepo, list[str]],
) -> None:
    repo, shas = history
    diff = repo.git.diff(shas[0], shas[1])
    assert diff.count("diff --git ") == 2
    assert "--- a/src/calc.py\n+++ b/src/calc.py\n" in diff
    assert "new file mode 100644\n" in diff
    index_line = next(line for line in diff.splitlines() if line.startswith("index "))
    old, new = index_line.split()[1].split("..")
    assert len(old) == len(new) == 40


def test_diff_can_be_limited_to_paths(history: tuple[FixtureRepo, list[str]]) -> None:
    repo, shas = history
    diff = repo.git.diff(shas[0], shas[1], paths=["tests/test_calc.py"])
    assert diff.startswith("diff --git a/tests/test_calc.py b/tests/test_calc.py\n")
    assert "src/calc.py" not in diff
    with pytest.raises(InvalidPathError):
        repo.git.diff(shas[0], shas[1], paths=["../outside"])
    with pytest.raises(RevisionNotFoundError):
        repo.git.diff("nope", shas[1])


def test_diff_emits_binary_patches(history: tuple[FixtureRepo, list[str]]) -> None:
    repo, shas = history
    diff = repo.git.diff(shas[1], shas[2])
    assert "GIT binary patch\n" in diff
    assert "diff --git a/logo.bin b/logo.bin\n" in diff


def test_diff_rename_detection_is_explicit(repo_factory: RepoFactory) -> None:
    body = "".join(f"line {i}\n" for i in range(20))
    repo = repo_factory.create(files={"pkg/old.py": body})
    before = repo.head
    repo.rename("pkg/old.py", "pkg/new.py")
    after = repo.commit("refactor: rename module")
    renamed = repo.git.diff(before, after)
    assert "rename from pkg/old.py\nrename to pkg/new.py\n" in renamed
    split = repo.git.diff(before, after, find_renames=False)
    assert "deleted file mode" in split
    assert "new file mode" in split


def test_diff_preserves_non_utf8_bytes(repo_factory: RepoFactory) -> None:
    repo = repo_factory.create(files={"latin1.txt": b"caf\xe9\n"})
    before = repo.head
    after = repo.commit("fix: accent", {"latin1.txt": b"caf\xe8\n"})
    raw = repo.git.diff(before, after).encode("utf-8", "surrogateescape")
    assert b"-caf\xe9\n+caf\xe8\n" in raw


def test_read_file_and_list_files_at_any_revision(
    history: tuple[FixtureRepo, list[str]],
) -> None:
    repo, shas = history
    git = repo.git
    assert git.read_file(shas[0], "src/calc.py") == b"def add(a, b):\n    return a - b\n"
    assert git.read_file("HEAD", "src/calc.py") == b"def add(a, b):\n    return a + b\n"
    assert git.read_file(shas[0], "tests/test_calc.py") is None
    assert git.read_file("HEAD", "src") is None
    with pytest.raises(InvalidPathError):
        git.read_file("HEAD", "/etc/passwd")
    assert git.list_files(shas[0]) == ("src/calc.py",)
    assert git.list_files("HEAD") == ("NOTES.md", "logo.bin", "src/calc.py", "tests/test_calc.py")


# --- clone and fetch -----------------------------------------------------------------


def test_clone_from_local_path(history: tuple[FixtureRepo, list[str]], tmp_path: Path) -> None:
    repo, shas = history
    clone = Git(tmp_path / "work" / "clone").clone_from(str(repo.path))
    assert clone.is_repo()
    assert clone.rev_parse("HEAD") == shas[-1]
    assert (clone.repo_dir / "src" / "calc.py").is_file()
    bare = Git(tmp_path / "bare.git").clone_from(str(repo.path), bare=True, no_checkout=True)
    assert bare.rev_parse("HEAD") == shas[-1]
    assert not (bare.repo_dir / "src").exists()
    shallow = Git(tmp_path / "shallow").clone_from(f"file://{repo.path}", depth=1)
    assert shallow.rev_parse("HEAD") == shas[-1]
    assert not shallow.has_commit(shas[0])


def test_clone_rejects_option_injection_and_disallowed_transports(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Git(tmp_path / "c").clone_from("--upload-pack=touch /tmp/x")
    with pytest.raises(ValueError, match="depth"):
        Git(tmp_path / "c").clone_from("https://example.invalid/a/b", depth=0)
    marker = tmp_path / "pwned"
    with pytest.raises(GitError):
        Git(tmp_path / "ext").clone_from(f"ext::sh -c touch% {marker}")
    assert not marker.exists()


def test_fetch_commit_fetches_one_sha_and_its_parent_only(
    history: tuple[FixtureRepo, list[str]], tmp_path: Path
) -> None:
    repo, shas = history
    cache = Git(tmp_path / "cache.git").init(bare=True)
    assert cache.fetch_commit(str(repo.path), shas[1].upper(), depth=2) == shas[1]
    assert cache.parents(shas[1]) == (shas[0],)
    assert cache.has_commit(shas[0])
    assert not cache.has_commit(shas[2])
    assert cache.read_file(shas[1], "tests/test_calc.py") is not None
    assert cache.diff(shas[0], shas[1]) == repo.git.diff(shas[0], shas[1])


def test_fetch_commit_errors(history: tuple[FixtureRepo, list[str]], tmp_path: Path) -> None:
    repo, _ = history
    cache = Git(tmp_path / "cache").init()
    with pytest.raises(InvalidRevisionError, match="full 40 or 64"):
        cache.fetch_commit(str(repo.path), "abc1234")
    with pytest.raises(GitError):
        cache.fetch_commit(str(repo.path), "f" * 40)
    with pytest.raises(ConfigError):
        cache.fetch("--upload-pack=evil", "main")
    with pytest.raises(ConfigError):
        cache.fetch(str(repo.path), "--force")
    with pytest.raises(ValueError, match="depth"):
        cache.fetch(str(repo.path), "main", depth=0)


def test_init_validates_branch_name(tmp_path: Path) -> None:
    with pytest.raises(InvalidRevisionError):
        Git(tmp_path / "r").init(initial_branch="-evil")
    git = Git(tmp_path / "r2").init(initial_branch="trunk")
    assert git.run("symbolic-ref", "HEAD").stdout_text.strip() == "refs/heads/trunk"
