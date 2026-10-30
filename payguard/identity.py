"""Run fingerprints, immutable lock markers, and episode-role access records."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterator, Mapping

import yaml

from triage.runbooks import load_triage_config


SCHEMA_VERSION = 1
CONFIG_FILES = (
    "configs/episodes.yaml",
    "configs/llm.yaml",
    "configs/model.yaml",
    "configs/monitoring.yaml",
    "configs/simulator.yaml",
    "configs/triage.yaml",
)
PROMPT_FILES = ("triage/agent.py", "triage/tools.py")
POLICY_PROVIDERS = {
    "rule_human": ("unconfigured", None),
    "rule_agent_fake": ("fake", "fake/payguard-p4-round1"),
}
ROLES = frozenset({"development", "calibration", "test"})


class RunIdentityError(ValueError):
    """Raised when a run or lock no longer identifies the current code/data."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git(*args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository_root(),
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunIdentityError("could not read Git identity for the run") from exc
    return result.stdout


def _dirty_entries() -> tuple[tuple[str, str], ...]:
    raw = _git("status", "--porcelain", "--untracked-files=all")
    entries: list[tuple[str, str]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if len(line) >= 4:
            entries.append((line[:2], line[3:]))
    return tuple(entries)


def _inside(path: str, parent: str | None) -> bool:
    return bool(parent and (path == parent or path.startswith(parent.rstrip("/") + "/")))


def _code_hash(
    head: str,
    dirty: bool,
    *,
    entries: tuple[tuple[str, str], ...],
    ignored_output: str | None,
) -> str:
    diff = _git("diff", "--binary", "HEAD")
    untracked: dict[str, str] = {}
    for status, relative in entries:
        if status != "??" or _inside(relative, ignored_output):
            continue
        path = repository_root() / relative
        if path.is_file():
            try:
                untracked[relative] = _sha256(path.read_bytes())
            except OSError as exc:
                raise RunIdentityError(f"could not hash untracked file {relative!r}") from exc
    return _sha256(
        _canonical(
            {
                "git_head": head,
                "git_dirty": dirty,
                "tracked_diff_sha256": _sha256(diff),
                "untracked_file_sha256": untracked,
            }
        )
    )


def current_hashes(*, benchmark_root: str | Path | None = None) -> dict[str, Any]:
    """Hash tracked code, configs, prompt sources, and effective sampling values."""
    root = repository_root()
    head = _git("rev-parse", "--verify", "HEAD").decode("ascii").strip().lower()
    if not head or any(char not in "0123456789abcdef" for char in head):
        raise RunIdentityError("Git HEAD is unavailable or invalid")

    output_root = Path(benchmark_root).expanduser().resolve() if benchmark_root else None
    entries = _dirty_entries()
    dirty_entries = entries
    ignored_output = None
    if output_root is not None:
        try:
            output_relative = output_root.relative_to(root).as_posix()
        except ValueError:
            output_relative = None
        if output_relative:
            ignored_output = output_relative
            dirty_entries = tuple(
                (status, path)
                for status, path in entries
                if not (status == "??" and _inside(path, output_relative))
            )
    dirty = bool(dirty_entries)

    config_hashes: dict[str, str] = {}
    for relative in CONFIG_FILES:
        path = root / relative
        try:
            config_hashes[relative] = _sha256(path.read_bytes())
        except OSError as exc:
            raise RunIdentityError(f"required config is missing: {relative}") from exc

    prompt_hashes: dict[str, str] = {}
    for relative in PROMPT_FILES:
        path = root / relative
        try:
            prompt_hashes[relative] = _sha256(path.read_bytes())
        except OSError as exc:
            raise RunIdentityError(f"required prompt source is missing: {relative}") from exc

    try:
        llm_raw = yaml.safe_load((root / "configs/llm.yaml").read_text(encoding="utf-8"))
        sampling_hash = _sha256(_canonical(llm_raw["sampling"]))
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise RunIdentityError("configs/llm.yaml sampling section is missing or invalid") from exc

    return {
        "git_head": head,
        "git_dirty": dirty,
        "code_hash": _code_hash(
            head,
            dirty,
            entries=dirty_entries,
            ignored_output=ignored_output,
        ),
        "config_hash": _sha256(_canonical(config_hashes)),
        "config_hashes": config_hashes,
        "prompt_hash": _sha256(_canonical(prompt_hashes)),
        "prompt_hashes": prompt_hashes,
        "sampling_hash": sampling_hash,
    }


def policy_identity(policy_name: str) -> dict[str, Any]:
    if policy_name not in POLICY_PROVIDERS:
        raise RunIdentityError(f"unsupported P5.1 policy: {policy_name!r}")
    provider_name, model_slug = POLICY_PROVIDERS[policy_name]
    triage_config_path = repository_root() / "configs/triage.yaml"
    triage_config = load_triage_config(triage_config_path)
    policy_version = triage_config.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise RunIdentityError("configs/triage.yaml has no valid policy_version")
    return {
        "policy_name": policy_name,
        "provider_name": provider_name,
        "policy_version": policy_version,
        "model_slug": model_slug,
    }


def current_run_identity(
    policy_name: str,
    *,
    benchmark_root: str | Path | None = None,
) -> dict[str, Any]:
    return {
        "hashes": current_hashes(benchmark_root=benchmark_root),
        **policy_identity(policy_name),
    }


def run_fingerprint(identity: Mapping[str, Any], *, base_fingerprint: str) -> str:
    return _sha256(_canonical({"identity": identity, "base_fingerprint": base_fingerprint}))


def _locks_directory(root: Path) -> Path:
    return root / "locks"


def _lock_file(root: Path, kind: str, policy_name: str | None = None) -> Path:
    if kind == "prompt-rule":
        if policy_name is not None:
            raise RunIdentityError("prompt-rule lock is shared and does not take --policy")
        return _locks_directory(root) / "prompt-rule.json"
    if kind == "policy":
        if policy_name not in POLICY_PROVIDERS:
            raise RunIdentityError("policy lock requires a supported --policy")
        return _locks_directory(root) / "policy" / f"{policy_name}.json"
    raise RunIdentityError(f"unknown lock kind: {kind!r}")


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def create_lock(
    benchmark_root: str | Path,
    kind: str,
    *,
    policy_name: str | None = None,
) -> Path:
    root = Path(benchmark_root).expanduser().resolve()
    dirty_entries = _dirty_entries()
    if dirty_entries:
        paths = ", ".join(path for _, path in dirty_entries[:5])
        raise RunIdentityError(
            "cannot create a lock while the Git working tree has uncommitted changes"
            + (f": {paths}" if paths else "")
        )
    hashes = current_hashes(benchmark_root=root)
    identity: dict[str, Any] = {"hashes": hashes}
    if kind == "policy":
        identity.update(policy_identity(policy_name or ""))
    lock_path = _lock_file(root, kind, policy_name)
    marker = {
        "schema_version": SCHEMA_VERSION,
        "lock_kind": kind,
        "locked_at": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "identity": identity,
    }
    if lock_path.exists():
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunIdentityError(f"existing lock marker is invalid: {lock_path}") from exc
        if existing.get("identity") != identity:
            raise RunIdentityError(
                f"lock already exists with different hashes: {lock_path}; use a new benchmark root"
            )
        return lock_path
    _atomic_write_json(lock_path, marker)
    return lock_path


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunIdentityError(f"required lock marker is missing or invalid: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise RunIdentityError(f"required lock marker has an unsupported schema: {path}")
    return value


def assert_role_access(
    benchmark_root: str | Path,
    *,
    operation: str,
    role_episode_ids: Mapping[str, tuple[str, ...] | list[str]],
    policy_name: str | None,
) -> None:
    root = Path(benchmark_root).expanduser().resolve()
    identity_hashes = current_hashes(benchmark_root=root)
    current_policy = policy_identity(policy_name) if policy_name else None
    for role, episode_ids in role_episode_ids.items():
        if role not in ROLES or not episode_ids:
            continue
        if role == "calibration":
            lock_path = _lock_file(root, "prompt-rule")
            marker = _read_lock(lock_path)
            if marker.get("lock_kind") != "prompt-rule":
                raise RunIdentityError(f"wrong lock kind in {lock_path}")
            locked_identity = marker.get("identity")
            if not isinstance(locked_identity, dict) or locked_identity.get("hashes") != identity_hashes:
                raise RunIdentityError(
                    "calibration analysis refused: current code/config/prompt/sampling hashes "
                    "do not match locks/prompt-rule.json"
                )
        elif role == "test":
            if current_policy is None:
                raise RunIdentityError("test analysis requires a selected --policy")
            lock_path = _lock_file(root, "policy", str(current_policy["policy_name"]))
            marker = _read_lock(lock_path)
            locked_identity = marker.get("identity")
            expected = {"hashes": identity_hashes, **current_policy}
            if marker.get("lock_kind") != "policy" or locked_identity != expected:
                raise RunIdentityError(
                    f"test analysis refused: current hashes/policy do not match {lock_path}"
                )
    record_role_touches(
        root,
        operation=operation,
        role_episode_ids=role_episode_ids,
        policy_name=policy_name,
        usage="analysis",
    )


@contextmanager
def _role_ledger_lock(root: Path) -> Iterator[None]:
    lock_path = root / "seed-role-ledger.json.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def record_role_touches(
    benchmark_root: str | Path,
    *,
    operation: str,
    role_episode_ids: Mapping[str, tuple[str, ...] | list[str]],
    policy_name: str | None,
    usage: str,
) -> None:
    root = Path(benchmark_root).expanduser().resolve()
    path = root / "seed-role-ledger.json"
    roles = {
        role: sorted(set(episode_ids))
        for role, episode_ids in sorted(role_episode_ids.items())
        if role in ROLES and episode_ids
    }
    if not roles:
        return
    entry_material = {
        "operation": operation,
        "policy_name": policy_name,
        "roles": roles,
        "usage": usage,
    }
    entry_id = _sha256(_canonical(entry_material))
    with _role_ledger_lock(root):
        if path.exists():
            try:
                ledger = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RunIdentityError(f"seed/role ledger is invalid: {path}") from exc
        else:
            ledger = {"schema_version": SCHEMA_VERSION, "entries": {}}
        if ledger.get("schema_version") != SCHEMA_VERSION or not isinstance(
            ledger.get("entries"), dict
        ):
            raise RunIdentityError(f"seed/role ledger has an unsupported schema: {path}")
        if entry_id not in ledger["entries"]:
            ledger["entries"][entry_id] = {
                **entry_material,
                "entry_id": entry_id,
                "touched_at": datetime.now(UTC).isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                ),
            }
            _atomic_write_json(path, ledger)


def find_harness_root(path: str | Path) -> Path | None:
    current = Path(path).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        marker = candidate / "harness-manifest.json"
        if marker.is_file():
            return candidate
    return None


def policy_name_for_run(run_dir: str | Path) -> str | None:
    path = Path(run_dir).expanduser().resolve() / "run-manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunIdentityError(f"policy run manifest is invalid: {path}") from exc
    name = manifest.get("policy_name")
    return name if isinstance(name, str) else None

