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

from triage.llm import load_llm_config
from triage.runbooks import load_triage_config


SCHEMA_VERSION = 1
CONFIG_FILES = (
    "configs/benchmark.yaml",
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
    "rule_agent_openrouter_k1": ("openrouter", None),
}
ROLES = frozenset({"development", "calibration", "test"})
LOCK_PROFILES = frozenset({"smoke", "benchmark"})
LOCK_PROFILE_ALIASES = {"pilot": "benchmark"}
LOCK_PROFILE_CHOICES = frozenset({*LOCK_PROFILES, *LOCK_PROFILE_ALIASES})
BENCHMARK_STATE_DIR_ENV = "PAYGUARD_BENCHMARK_STATE_DIR"


class RunIdentityError(ValueError):
    """Raised when a run or lock no longer identifies the current code/data."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def benchmark_state_directory(
    benchmark_root: str | Path | None = None,
    *,
    create: bool = True,
) -> Path:
    """Resolve the shared cost/role/lock directory, independent of run identity."""
    configured = os.environ.get(BENCHMARK_STATE_DIR_ENV)
    if configured is not None:
        candidate = Path(configured).expanduser()
        if not configured.strip() or not candidate.is_absolute():
            raise RunIdentityError(
                f"{BENCHMARK_STATE_DIR_ENV} must be an absolute path when set"
            )
        state_dir = candidate.resolve()
    else:
        state_dir = (Path.home() / ".payguard-benchmark").resolve()
    if benchmark_root is not None:
        root = Path(benchmark_root).expanduser().resolve()
        try:
            state_dir.relative_to(root)
        except ValueError:
            pass
        else:
            raise RunIdentityError(
                f"shared benchmark state directory must be outside benchmark root {root}"
            )
        try:
            root.relative_to(state_dir)
        except ValueError:
            pass
        else:
            raise RunIdentityError(
                f"benchmark root must be outside shared benchmark state directory {state_dir}"
            )
    if create:
        try:
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise RunIdentityError(
                f"cannot create shared benchmark state directory {state_dir}: {exc}"
            ) from exc
    return state_dir


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


def _runtime_code_hash(root: Path) -> str:
    """Hash runtime Python source bytes without Git metadata or documentation."""
    files: dict[str, str] = {}
    for package in ("evaluation", "model", "monitoring", "payguard", "simulator", "triage"):
        package_root = root / package
        if not package_root.is_dir():
            continue
        for path in sorted(package_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            try:
                files[relative] = _sha256(path.read_bytes())
            except OSError as exc:
                raise RunIdentityError(f"could not hash runtime source {relative!r}") from exc
    if not files:
        raise RunIdentityError("no runtime Python source files were found")
    return _sha256(_canonical(files))


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
        "code_hash": _runtime_code_hash(root),
        "config_hash": _sha256(_canonical(config_hashes)),
        "config_hashes": config_hashes,
        "prompt_hash": _sha256(_canonical(prompt_hashes)),
        "prompt_hashes": prompt_hashes,
        "sampling_hash": sampling_hash,
    }


def lock_identity_hashes(hashes: Mapping[str, Any]) -> dict[str, Any]:
    """Keep held-out locks stable across docs-only commits while hashing runtime inputs."""
    keys = (
        "code_hash",
        "config_hash",
        "config_hashes",
        "prompt_hash",
        "prompt_hashes",
        "sampling_hash",
    )
    missing = [key for key in keys if key not in hashes]
    if missing:
        raise RunIdentityError(f"current identity is missing lock hashes: {missing!r}")
    return {key: hashes[key] for key in keys}


def normalize_lock_profile(profile: str) -> str:
    """Map simulator profile names to the shared lock namespace."""
    if not isinstance(profile, str):
        raise RunIdentityError(f"unsupported lock profile: {profile!r}")
    normalized = LOCK_PROFILE_ALIASES.get(profile, profile)
    if normalized not in LOCK_PROFILES:
        raise RunIdentityError(f"unsupported lock profile: {profile!r}")
    return normalized


def identity_hash_mismatches(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return content hash fields that differ, ignoring informational Git metadata."""
    expected_hashes = lock_identity_hashes(expected)
    actual_hashes = lock_identity_hashes(actual)
    mismatches: list[str] = []
    for field in ("code_hash", "config_hash", "prompt_hash", "sampling_hash"):
        if expected_hashes[field] != actual_hashes[field]:
            mismatches.append(field)
    for field in ("config_hashes", "prompt_hashes"):
        expected_values = expected_hashes[field]
        actual_values = actual_hashes[field]
        if not isinstance(expected_values, Mapping) or not isinstance(actual_values, Mapping):
            mismatches.append(field)
            continue
        for name in sorted(set(expected_values) | set(actual_values)):
            if expected_values.get(name) != actual_values.get(name):
                mismatches.append(f"{field}[{name}]")
    return tuple(mismatches)


def _harness_profile(root: Path) -> str:
    manifest_path = root / "harness-manifest.json"
    if not manifest_path.is_file():
        for candidate in (root / "generation-manifest.json", root / "base" / "generation-manifest.json"):
            if candidate.is_file():
                try:
                    manifest = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RunIdentityError(
                        f"generation manifest is missing or invalid: {candidate}"
                    ) from exc
                profile = manifest.get("profile") if isinstance(manifest, dict) else None
                if isinstance(profile, str):
                    return normalize_lock_profile(profile)
        return "smoke"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunIdentityError(f"harness manifest is missing or invalid: {manifest_path}") from exc
    profile = manifest.get("profile") if isinstance(manifest, dict) else None
    if not isinstance(profile, str):
        raise RunIdentityError(f"harness manifest has an unsupported lock profile: {profile!r}")
    return normalize_lock_profile(profile)


def assert_harness_state_directory(
    benchmark_root: str | Path,
    manifest: Mapping[str, Any] | None = None,
) -> Path:
    """Reject a harness root resumed with a different shared-state location."""
    root = Path(benchmark_root).expanduser().resolve()
    manifest_path = root / "harness-manifest.json"
    if manifest is None:
        try:
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunIdentityError(f"harness manifest is missing or invalid: {manifest_path}") from exc
        if not isinstance(manifest_value, dict):
            raise RunIdentityError(f"harness manifest must contain a JSON object: {manifest_path}")
        manifest = manifest_value
    state_dir = benchmark_state_directory(root, create=False)
    recorded = manifest.get("state_dir")
    if not isinstance(recorded, str) or not recorded:
        raise RunIdentityError(
            "harness manifest does not record its resolved benchmark state directory"
        )
    try:
        recorded_dir = Path(recorded).expanduser()
        if not recorded_dir.is_absolute():
            raise ValueError("recorded path is relative")
        recorded_dir = recorded_dir.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RunIdentityError("harness manifest has an invalid benchmark state directory") from exc
    if recorded_dir != state_dir:
        raise RunIdentityError(
            "benchmark state directory mismatch: current setting resolves to "
            f"{state_dir}, but harness-manifest.json records {recorded_dir}; "
            "changing the state directory does not reset benchmark spend or locks"
        )
    return state_dir


def policy_identity(policy_name: str) -> dict[str, Any]:
    if policy_name not in POLICY_PROVIDERS:
        raise RunIdentityError(f"unsupported P5.1 policy: {policy_name!r}")
    provider_name, model_slug = POLICY_PROVIDERS[policy_name]
    if provider_name == "openrouter":
        model_slug = load_llm_config(repository_root() / "configs/llm.yaml").canonical_slug
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
    material = dict(identity)
    hashes = material.get("hashes")
    if isinstance(hashes, Mapping):
        material["hashes"] = lock_identity_hashes(hashes)
    return _sha256(_canonical({"identity": material, "base_fingerprint": base_fingerprint}))


def _lock_file(
    root: Path,
    kind: str,
    policy_name: str | None = None,
    *,
    profile: str,
) -> Path:
    profile = normalize_lock_profile(profile)
    state_dir = benchmark_state_directory(root, create=False)
    if kind == "prompt-rule":
        if policy_name is not None:
            raise RunIdentityError("prompt-rule lock is shared and does not take --policy")
        return state_dir / "locks" / profile / "prompt-rule.json"
    if kind == "policy":
        if policy_name not in POLICY_PROVIDERS:
            raise RunIdentityError("policy lock requires a supported --policy")
        return state_dir / "locks" / profile / "policy" / f"{policy_name}.json"
    raise RunIdentityError(f"unknown lock kind: {kind!r}")


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        )
    temporary.replace(path)


def create_lock(
    benchmark_root: str | Path,
    kind: str,
    *,
    policy_name: str | None = None,
    profile: str = "smoke",
) -> Path:
    root = Path(benchmark_root).expanduser().resolve()
    state_dir = benchmark_state_directory(root, create=False)
    profile = normalize_lock_profile(profile)
    harness_manifest_path = root / "harness-manifest.json"
    has_profile_manifest = harness_manifest_path.is_file() or any(
        (root / name).is_file()
        for name in ("generation-manifest.json", "base/generation-manifest.json")
    )
    if harness_manifest_path.is_file():
        assert_harness_state_directory(root)
    if has_profile_manifest:
        root_profile = _harness_profile(root)
        if profile != root_profile:
            raise RunIdentityError(
                f"lock profile {profile!r} does not match run manifest profile {root_profile!r}"
            )
    dirty_entries = _dirty_entries()
    if dirty_entries:
        paths = ", ".join(path for _, path in dirty_entries[:5])
        raise RunIdentityError(
            "cannot create a lock while the Git working tree has uncommitted changes"
            + (f": {paths}" if paths else "")
        )
    hashes = lock_identity_hashes(current_hashes(benchmark_root=root))
    identity: dict[str, Any] = {"hashes": hashes}
    if kind == "policy":
        identity.update(policy_identity(policy_name or ""))
    lock_path = _lock_file(root, kind, policy_name, profile=profile)
    marker = {
        "schema_version": SCHEMA_VERSION,
        "lock_kind": kind,
        "profile": profile,
        "locked_at": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "identity": identity,
    }
    if lock_path.exists():
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunIdentityError(f"existing lock marker is invalid: {lock_path}") from exc
        if existing.get("profile") != profile or existing.get("identity") != identity:
            raise RunIdentityError(
                f"benchmark lock already exists for profile {profile!r} with different "
                f"hashes: {lock_path}; a new benchmark root does not reset locks"
            )
        return lock_path
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def verify_role_locks(
    benchmark_root: str | Path,
    *,
    operation: str,
    role_episode_ids: Mapping[str, tuple[str, ...] | list[str]],
    policy_name: str | None,
) -> None:
    """Verify calibration/test locks without recording a role-ledger touch."""
    root = Path(benchmark_root).expanduser().resolve()
    manifest_path = root / "harness-manifest.json"
    if manifest_path.is_file():
        assert_harness_state_directory(root)
    profile = _harness_profile(root)
    current_hashes_value = current_hashes(benchmark_root=root)
    identity_hashes = lock_identity_hashes(current_hashes_value)
    current_policy = policy_identity(policy_name) if policy_name else None
    for role, episode_ids in role_episode_ids.items():
        if role not in ROLES or not episode_ids:
            continue
        if role == "calibration":
            lock_path = _lock_file(root, "prompt-rule", profile=profile)
            marker = _read_lock(lock_path)
            if marker.get("lock_kind") != "prompt-rule" or marker.get("profile") != profile:
                raise RunIdentityError(f"wrong lock kind in {lock_path}")
            locked_identity = marker.get("identity")
            if not isinstance(locked_identity, dict) or locked_identity.get("hashes") != identity_hashes:
                differences = (
                    identity_hash_mismatches(locked_identity.get("hashes", {}), current_hashes_value)
                    if isinstance(locked_identity, dict)
                    else ()
                )
                raise RunIdentityError(
                    "calibration analysis refused: current code/config/prompt/sampling hashes "
                    f"do not match locks/{profile}/prompt-rule.json; differing hashes: "
                    f"{', '.join(differences) or 'invalid lock identity'}"
                )
        elif role == "test":
            if current_policy is None:
                raise RunIdentityError("test analysis requires a selected --policy")
            lock_path = _lock_file(
                root,
                "policy",
                str(current_policy["policy_name"]),
                profile=profile,
            )
            marker = _read_lock(lock_path)
            locked_identity = marker.get("identity")
            expected = {"hashes": identity_hashes, **current_policy}
            if (
                marker.get("lock_kind") != "policy"
                or marker.get("profile") != profile
                or locked_identity != expected
            ):
                differences = (
                    identity_hash_mismatches(locked_identity.get("hashes", {}), current_hashes_value)
                    if isinstance(locked_identity, dict)
                    else ()
                )
                raise RunIdentityError(
                    f"test analysis refused: current hashes/policy do not match {lock_path}; "
                    f"differing hashes: {', '.join(differences) or 'policy identity'}"
                )


def assert_role_access(
    benchmark_root: str | Path,
    *,
    operation: str,
    role_episode_ids: Mapping[str, tuple[str, ...] | list[str]],
    policy_name: str | None,
) -> None:
    root = Path(benchmark_root).expanduser().resolve()
    verify_role_locks(
        root,
        operation=operation,
        role_episode_ids=role_episode_ids,
        policy_name=policy_name,
    )
    record_role_touches(
        root,
        operation=operation,
        role_episode_ids=role_episode_ids,
        policy_name=policy_name,
        usage="analysis",
    )


@contextmanager
def _role_ledger_lock(state_dir: Path) -> Iterator[None]:
    lock_path = state_dir / "seed-role-ledger.json.lock"
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
    if (root / "harness-manifest.json").is_file():
        assert_harness_state_directory(root)
    state_dir = benchmark_state_directory(root)
    path = state_dir / "seed-role-ledger.json"
    roles = {
        role: sorted(set(episode_ids))
        for role, episode_ids in sorted(role_episode_ids.items())
        if role in ROLES and episode_ids
    }
    if not roles:
        return
    entry_material = {
        "benchmark_root": str(root),
        "code_identity_hash": _sha256(
            _canonical(lock_identity_hashes(current_hashes(benchmark_root=root)))
        ),
        "operation": operation,
        "policy_name": policy_name,
        "roles": roles,
        "usage": usage,
    }
    entry_id = _sha256(_canonical(entry_material))
    with _role_ledger_lock(state_dir):
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

