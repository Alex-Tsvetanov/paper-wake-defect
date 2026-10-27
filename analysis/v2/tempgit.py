"""Throwaway git repositories for the tests of the provenance rule (plan.py, pilot.py). Tests only."""

import os
import shutil
import stat
import subprocess
import tempfile
from unittest import mock


def remove_tree(path):
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(folder, name), stat.S_IWRITE)  # git's objects are read-only
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


class TempGit:
    """Throwaway repositories for the real-plan tests: outside the worktree, hermetic, never the real one.

    No global or system git configuration (so none of the user's identity, signing, hooks or
    line-end conversion), and git cannot climb from the temporary folder into another repository.
    The environment is patched for the whole test, so plan.py's own git calls see the same.
    """

    def __init__(self, case):
        self.root = tempfile.mkdtemp(prefix="p1v2-git-")
        case.addCleanup(remove_tree, self.root)
        empty = os.path.join(self.root, "empty.gitconfig")
        open(empty, "w").close()
        patch = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": empty, "GIT_CONFIG_NOSYSTEM": "1",
                                             "GIT_CEILING_DIRECTORIES": self.root})
        patch.start()
        case.addCleanup(patch.stop)
        self.repo = self.init("repo")

    def git(self, repo, *args):
        done = subprocess.run(["git", "-C", repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"] + list(args),
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), done.stderr))
        return done.stdout.strip()

    def init(self, name):
        path = os.path.join(self.root, name)
        os.mkdir(path)
        self.git(path, "init", "-q")
        return path

    def write(self, repo, name, text):
        with open(os.path.join(repo, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return os.path.join(repo, name)

    def commit_all(self, repo):
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "test")

    def head(self, repo):
        return self.git(repo, "rev-parse", "HEAD")
