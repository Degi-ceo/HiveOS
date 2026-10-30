"""Opt-in host launcher for a selected, revalidated gateway release.

Install this module and its interpreter in a host-controlled environment, not
in a release checkout. This launcher does not select releases, create deploy
receipts, restart services, or authorize rollback.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hive.core.deployment_identity import deployment_host_key
from hive.core.deployment_ledger import DeployLedger
from hive.core.host_selection import read_host_selected_sha
from hive.core.installed_release import InstalledReleaseSelector

_GATEWAY_BOOTSTRAP = (
    "import importlib.util,runpy,sys; from pathlib import Path; "
    "source,expected=sys.argv[1:3]; "
    "sys.path.insert(0,source); "
    "names=('hive','hive.surfaces','hive.surfaces.cli'); "
    "origins=(Path(expected),Path(source)/'hive/surfaces/__init__.py',"
    "Path(source)/'hive/surfaces/cli/__init__.py'); "
    "specs=(importlib.util.find_spec(name) for name in names); "
    "all(spec is not None and isinstance(spec.origin,str) "
    "and Path(spec.origin).resolve()==origin.resolve() "
    "for spec,origin in zip(specs,origins,strict=True)) or sys.exit(78); "
    "sys.argv=['hive','serve']; "
    "runpy.run_module('hive.surfaces.cli',run_name='__main__')"
)
_REQUIRED_GATEWAY_PATHS = (
    "HIVE_DATA_DIR", "HIVE_STATE_DB", "OBSIDIAN_VAULT_PATH", "HIVE_ENV_FILE",
)


@dataclass(frozen=True, slots=True)
class GatewayLaunch:
    """Exact process image and source tree selected for one gateway start."""

    sha: str
    root: Path
    executable: Path
    argv: tuple[str, ...]


def prepare_gateway_launch(
    selector: InstalledReleaseSelector, *, interpreter: str | Path | None = None,
) -> GatewayLaunch:
    """Resolve and verify selection before constructing fixed, shell-free argv."""
    sha, root = selector.selected_release()
    candidate = Path(sys.executable if interpreter is None else interpreter)
    if not candidate.is_absolute() or not candidate.is_file():
        raise ValueError("gateway interpreter must be an installed absolute file")
    resolved = candidate.resolve(strict=True)
    if candidate.is_relative_to(root.parent) or resolved.is_relative_to(root.parent):
        raise ValueError("gateway interpreter must be outside installed releases")
    source = root / "src"
    required = (
        source / "hive" / "__init__.py",
        source / "hive" / "surfaces" / "__init__.py",
        source / "hive" / "surfaces" / "cli" / "__init__.py",
        source / "hive" / "surfaces" / "cli" / "__main__.py",
    )
    if not all(path.is_file() for path in required):
        raise ValueError("selected release has no complete gateway CLI package")
    return GatewayLaunch(
        sha=sha,
        root=root,
        executable=candidate,
        argv=(str(candidate), "-I", "-B", "-c", _GATEWAY_BOOTSTRAP,
              str(source), str(required[0])),
    )


def gateway_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Keep service configuration while excluding interpreter/loader overrides."""
    env = os.environ if source is None else source
    launch_env = {
        name: value for name, value in env.items()
        if not name.upper().startswith(("PYTHON", "LD_", "DYLD_"))
    }
    launch_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return launch_env


def validate_gateway_paths(
    release_root: Path, state_db: Path, env: Mapping[str, str],
) -> dict[str, str]:
    """Keep every default-mutable gateway path out of the verified tree."""
    for name in _REQUIRED_GATEWAY_PATHS:
        raw = env.get(name)
        if not raw:
            raise ValueError("gateway data paths must be explicitly configured")
        path = Path(raw)
        if not path.is_absolute() or path.resolve().is_relative_to(release_root):
            raise ValueError("gateway data paths must be outside installed releases")
        if name == "HIVE_ENV_FILE" and path.name != ".env":
            raise ValueError("gateway environment file path must end in .env")
    if Path(env["HIVE_STATE_DB"]).resolve() != state_db.resolve():
        raise ValueError("gateway and selector databases must match")
    mnemosyne = env.get("MNEMOSYNE_HOME")
    if mnemosyne is None:
        mnemosyne = str(Path(env["HIVE_DATA_DIR"]) / "mnemosyne")
    if not mnemosyne:
        raise ValueError("gateway memory path must be configured outside installed releases")
    path = Path(mnemosyne)
    if not path.is_absolute() or path.resolve().is_relative_to(release_root):
        raise ValueError("gateway memory path must be outside installed releases")
    # Pin this explicit value in the child before dotenv loads; otherwise an
    # external .env could override the absent variable with a release path.
    return {"MNEMOSYNE_HOME": str(path.resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch one selected HiveOS gateway release")
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--host-key", required=True)
    parser.add_argument("--scope", choices=("system", "user"), required=True)
    parser.add_argument("--selection-credential", default="")
    options = parser.parse_args(argv)
    release_root = Path(options.release_root)
    state_db = Path(options.state_db)
    if not release_root.is_absolute() or not state_db.is_absolute():
        parser.exit(1, "selected release launch refused\n")
    try:
        root = release_root.resolve(strict=True)
        if state_db.resolve().is_relative_to(root):
            raise ValueError("state database must be outside installed releases")
        environment = dict(os.environ)
        pinned = validate_gateway_paths(root, state_db, environment)
        if not options.selection_credential and environment.get("HIVE_INSTALLED_RELEASE_CREDENTIAL"):
            raise ValueError("host selection credential requires launcher opt-in")
        host_key = (deployment_host_key(root) if options.host_key == "auto"
                    else options.host_key)
        if options.selection_credential and host_key != deployment_host_key(root):
            raise ValueError("host selection requires the stable catalog host key")
        selector = InstalledReleaseSelector(
            release_root, DeployLedger(state_db), host_key=host_key,
            scope=options.scope,
        )
        launch = prepare_gateway_launch(selector)
        # The selected source tree must also own SOUL, the approval gate, and
        # file-containment roots, regardless of inherited service environment.
        pinned["HIVE_REPO_ROOT"] = str(launch.root)
        if options.selection_credential:
            if read_host_selected_sha(options.selection_credential,
                                      environment=environment) != launch.sha:
                raise ValueError("selected release differs from host credential")
            pinned["HIVE_INSTALLED_RELEASE_CREDENTIAL"] = options.selection_credential
            pinned["HIVE_INSTALLED_RELEASE_ROOT"] = str(root)
            pinned["HIVE_DEPLOY_SYSTEMCTL_SCOPE"] = options.scope
        os.chdir(launch.root)
        os.execve(str(launch.executable), launch.argv,
                  {**gateway_environment(environment), **pinned})
    except (OSError, ValueError, sqlite3.DatabaseError):
        parser.exit(1, "selected release launch refused\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
