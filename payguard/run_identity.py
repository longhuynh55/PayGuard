"""Validation of persisted P5 harness identities without evaluator imports."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from payguard.identity import (
    RunIdentityError,
    assert_harness_state_directory,
    benchmark_state_directory,
    current_run_identity,
    find_harness_root,
    identity_hash_mismatches,
    lock_identity_hashes,
    normalize_lock_profile,
    policy_identity,
    policy_name_for_run,
    run_fingerprint,
    verify_role_locks,
)
from simulator.reproducibility import canonical_run_fingerprint


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunIdentityError(f"run identity manifest is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise RunIdentityError(f"run identity manifest must be a JSON object: {path}")
    return value


def _fingerprint(database_path: Path) -> str:
    if not database_path.is_file():
        raise RunIdentityError(f"required operational database is missing: {database_path}")
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return canonical_run_fingerprint(
            connection,
            empty_tables=("incidents", "decisions"),
        )
    except sqlite3.Error as exc:
        raise RunIdentityError(f"could not fingerprint {database_path}: {exc}") from exc
    finally:
        connection.close()


def _assert_base_is_frozen(database_path: Path, expected_fingerprint: str) -> None:
    if not database_path.is_file():
        raise RunIdentityError(f"frozen BASE database is missing: {database_path}")
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        output_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("incidents", "decisions")
        }
    except sqlite3.Error as exc:
        raise RunIdentityError(f"could not validate frozen BASE {database_path}: {exc}") from exc
    finally:
        connection.close()
    actual_fingerprint = _fingerprint(database_path)
    if any(output_counts.values()) or actual_fingerprint != expected_fingerprint:
        raise RunIdentityError(
            "frozen BASE changed: expected empty incidents/decisions and input "
            f"fingerprint {expected_fingerprint}, got {output_counts} / {actual_fingerprint}"
        )


def _check_hash_identity(
    expected: Mapping[str, Any], actual: Mapping[str, Any], *, label: str
) -> None:
    differences = identity_hash_mismatches(expected, actual)
    if differences:
        raise RunIdentityError(
            f"{label} resume refused: runtime/config/prompt/sampling hashes differ: "
            f"{', '.join(differences)}"
        )


def _assert_policy_lock_matches_manifest(
    benchmark_root: Path,
    manifest: Mapping[str, Any],
    roles: Sequence[str],
) -> None:
    if not roles:
        return
    selected = {role: ["role-check"] for role in roles}
    verify_role_locks(
        benchmark_root,
        operation="evaluation.identity_check",
        role_episode_ids=selected,
        policy_name=str(manifest["policy_name"]),
    )
    profile = normalize_lock_profile(str(_read_json(benchmark_root / "harness-manifest.json")["profile"]))
    state_dir = benchmark_state_directory(benchmark_root, create=False)
    run_hashes = manifest.get("hashes")
    if not isinstance(run_hashes, dict):
        raise RunIdentityError("policy run-manifest.json has no hashes object")
    run_lock_hashes = lock_identity_hashes(run_hashes)
    for role in roles:
        if role == "development":
            continue
        if role == "calibration":
            lock_path = state_dir / "locks" / profile / "prompt-rule.json"
        elif role == "test":
            lock_path = (
                state_dir
                / "locks"
                / profile
                / "policy"
                / f"{manifest['policy_name']}.json"
            )
        else:
            raise RunIdentityError(f"unsupported episode role for harness evaluation: {role!r}")
        marker = _read_json(lock_path)
        locked_identity = marker.get("identity")
        if not isinstance(locked_identity, dict):
            raise RunIdentityError(f"lock marker has no identity object: {lock_path}")
        locked_hashes = locked_identity.get("hashes")
        if locked_hashes != run_lock_hashes:
            differences = (
                identity_hash_mismatches(locked_hashes, run_hashes)
                if isinstance(locked_hashes, dict)
                else ()
            )
            raise RunIdentityError(
                f"harness evaluation refused: run-manifest hashes do not match {lock_path}; "
                f"differing hashes: {', '.join(differences) or 'invalid lock identity'}"
            )
        if role == "test":
            for key in ("policy_name", "provider_name", "policy_version", "model_slug"):
                if locked_identity.get(key) != manifest.get(key):
                    raise RunIdentityError(
                        f"harness evaluation refused: run-manifest {key} does not match "
                        f"test lock {lock_path}"
                    )


def verify_harness_policy_run(
    policy_dir: str | Path,
    *,
    required_roles: Sequence[str] = (),
) -> dict[str, Any]:
    """Check run, BASE, current inputs, optional held-out locks, and policy copy."""
    run_path = Path(policy_dir).expanduser().resolve()
    benchmark_root = find_harness_root(run_path)
    if benchmark_root is None:
        raise RunIdentityError(f"not a P5 harness policy run: {run_path}")
    policy_name = policy_name_for_run(run_path)
    if not policy_name or run_path.parent != benchmark_root / "policies" or run_path.name != policy_name:
        raise RunIdentityError("P5 harness triage requires a policy run directory")

    harness_manifest = _read_json(benchmark_root / "harness-manifest.json")
    assert_harness_state_directory(benchmark_root, harness_manifest)
    current_identity = current_run_identity(policy_name, benchmark_root=benchmark_root)
    current_hashes = current_identity["hashes"]
    harness_hashes = harness_manifest.get("hashes")
    if not isinstance(harness_hashes, dict):
        raise RunIdentityError("harness-manifest.json has no hashes object")
    _check_hash_identity(harness_hashes, current_hashes, label="harness")

    base_dir = benchmark_root / "base"
    base_manifest = _read_json(base_dir / "base-manifest.json")
    base_hashes = base_manifest.get("hashes")
    if not isinstance(base_hashes, dict):
        raise RunIdentityError("BASE manifest has no hashes object")
    _check_hash_identity(base_hashes, current_hashes, label="BASE")
    base_fingerprint = base_manifest.get("input_fingerprint")
    if not isinstance(base_fingerprint, str) or len(base_fingerprint) != 64:
        raise RunIdentityError("BASE manifest has no valid input fingerprint")
    base_database = base_dir / "operational" / "observations.sqlite"
    _assert_base_is_frozen(base_database, base_fingerprint)

    actual_manifest = _read_json(run_path / "run-manifest.json")
    run_hashes = actual_manifest.get("hashes")
    if not isinstance(run_hashes, dict):
        raise RunIdentityError("policy run-manifest.json has no hashes object")
    _assert_policy_lock_matches_manifest(benchmark_root, actual_manifest, required_roles)
    _check_hash_identity(run_hashes, current_hashes, label="policy")
    for key in ("policy_name", "provider_name", "policy_version", "model_slug"):
        if actual_manifest.get(key) != current_identity.get(key):
            raise RunIdentityError(
                f"policy resume refused: run-manifest {key} differs from current policy identity"
            )
    if actual_manifest.get("base_fingerprint") != base_fingerprint:
        raise RunIdentityError("policy run-manifest BASE fingerprint differs from frozen BASE")
    if actual_manifest.get("run_fingerprint") != run_fingerprint(
        {**current_identity, "hashes": lock_identity_hashes(current_hashes)},
        base_fingerprint=base_fingerprint,
    ):
        raise RunIdentityError("policy resume refused: run-manifest fingerprint is inconsistent")

    operational_path = run_path / "operational" / "observations.sqlite"
    if operational_path.is_file():
        actual_fingerprint = _fingerprint(operational_path)
        if actual_fingerprint != base_fingerprint:
            raise RunIdentityError(
                f"policy {policy_name!r} input fingerprint differs from frozen BASE"
            )
        connection = sqlite3.connect(
            f"{operational_path.resolve().as_uri()}?mode=ro", uri=True
        )
        try:
            rows = connection.execute(
                "SELECT DISTINCT source, policy_version, model_slug FROM decisions "
                "ORDER BY source, policy_version, model_slug"
            ).fetchall()
        except sqlite3.Error as exc:
            raise RunIdentityError(f"could not validate policy decisions: {exc}") from exc
        finally:
            connection.close()
        mismatches = [
            (str(row[0]), row[1], row[2])
            for row in rows
            if row[1] != actual_manifest.get("policy_version")
            or (row[0] == "agent" and row[2] != actual_manifest.get("model_slug"))
            or (row[0] == "rule" and row[2] is not None)
        ]
        if mismatches:
            raise RunIdentityError(
                "policy resume refused: decision rows do not match run-manifest.json; "
                f"found {mismatches!r}"
            )
    return actual_manifest
