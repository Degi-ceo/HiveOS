"""
sandbox.py — optional containerized runner for self-mod test runs (M5 #hd-3).

OpenJarvis sandbox pattern: when self-modifying, run the candidate's test suite inside
an isolated container so a broken or hostile edit can't touch the host. This is a thin
wrapper that produces a SelfModifier `Runner`:

  make_sandbox_runner(image="python:3.12") -> Runner

When an image is configured, commands run as `docker run --rm -v <root>:/repo -w /repo
<image> sh -c "<cmd>"`; with no image (or Docker unavailable) it degrades to the local
runner. Injectable + the command-wrapping is pure, so it is unit-testable without Docker.

DAG: core leaf — depends only on core.self_mod's Runner type + stdlib.
"""
from __future__ import annotations

import shlex

from hive.core.self_mod import Runner, _default_run


def docker_command(image: str, repo_root: str, cmd: str, *,
                   network: str = "none") -> str:
    """The `docker run` invocation that runs `cmd` against repo_root inside `image`.

    network=none by default: candidate tests get no network (tighter isolation)."""
    mount = f"{shlex.quote(repo_root)}:/repo"
    return (f"docker run --rm --network {shlex.quote(network)} "
            f"-v {shlex.quote(mount)} -w /repo {shlex.quote(image)} "
            f"sh -lc {shlex.quote(cmd)}")


def make_sandbox_runner(image: str | None = None, *, repo_root: str = ".",
                        network: str = "none", base: Runner | None = None) -> Runner:
    """Return a Runner that wraps test/build commands in a container when `image` is
    set; otherwise the plain local runner. git commands always run locally (they touch
    the host worktree); only the test command is sandboxed."""
    local = base or _default_run
    if not image:
        return local

    async def run(cmd: str, cwd: str | None = None) -> tuple[int, str]:
        # git plumbing must run on the host worktree, not in the container.
        if cmd.strip().startswith("git "):
            return await local(cmd, cwd)
        wrapped = docker_command(image, cwd or repo_root, cmd, network=network)
        return await local(wrapped, cwd)

    return run
