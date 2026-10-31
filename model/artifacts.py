"""Content identities for the trained model artifacts used by evaluation."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


MODEL_ARTIFACT_FILES = (
    "calibrator.joblib",
    "conformal.joblib",
    "metadata.json",
    "predictor.joblib",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def model_artifact_identity(model_directory: str | Path) -> dict[str, Any]:
    """Hash every persisted model artifact that can affect evaluation or provenance."""
    directory = Path(model_directory).expanduser().resolve()
    file_hashes: dict[str, str] = {}
    for name in MODEL_ARTIFACT_FILES:
        path = directory / name
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"required model artifact is missing or unreadable: {path}") from exc
        file_hashes[name] = sha256(payload).hexdigest()
    material = {"schema_version": 1, "files": file_hashes}
    return {**material, "content_sha256": sha256(_canonical(material)).hexdigest()}


def assert_model_artifact_identity(
    model_directory: str | Path,
    expected: Mapping[str, Any],
    *,
    label: str = "model artifacts",
) -> dict[str, Any]:
    """Reject missing or changed model inputs with the differing artifact names."""
    if expected.get("schema_version") != 1 or not isinstance(expected.get("files"), Mapping):
        raise ValueError(f"{label} identity is missing or invalid")
    actual = model_artifact_identity(model_directory)
    expected_files = expected["files"]
    differences = [
        name
        for name in MODEL_ARTIFACT_FILES
        if expected_files.get(name) != actual["files"].get(name)
    ]
    if expected.get("content_sha256") != actual["content_sha256"] and not differences:
        differences.append("content_sha256")
    if differences:
        raise ValueError(
            f"{label} content hashes differ: {', '.join(differences)}"
        )
    return actual
