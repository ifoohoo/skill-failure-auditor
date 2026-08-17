from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
NORMALIZER = (
    PACKAGE_ROOT
    / "plugin-src"
    / "platforms"
    / "codex"
    / "scripts"
    / "codex_artifact_normalizer.py"
)
ENGINE = PACKAGE_ROOT / "plugin-src" / "core" / "scripts" / "orchestration_engine.py"
BUILDER = PACKAGE_ROOT / "scripts" / "build" / "build_platforms.py"
GENERATED = PACKAGE_ROOT / "generated" / "platforms"

STATIC_ROLES = [
    "scope-routing",
    "static-audit",
    "evaluation-integrity",
    "adversarial-challenge",
    "result-synthesis",
]


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def create_package(root: Path) -> tuple[Path, Path]:
    output_root = root / "run"
    work = output_root / "work"
    work.mkdir(parents=True)
    package = {
        "task_id": "AUDIT-CODEX-NORMALIZER-TEST",
        "platform": "codex",
        "output_root": str(output_root.resolve()),
        "expected_roles": STATIC_ROLES,
        "package_digest": "",
    }
    package["package_digest"] = sha256_bytes(canonical(package))
    package_path = output_root / "task-package.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    return package_path, work


def base_artifact() -> dict:
    return {
        "schema_version": "1.0",
        "task_id": "AUDIT-CODEX-NORMALIZER-TEST",
        "platform": "codex",
        "role": "scope-routing",
        "semantic_status": "PASS_WITHIN_FROZEN_SCOPE",
        "conclusion_ceiling": "PASS_WITHIN_FROZEN_SCOPE",
        "rule_results": [],
        "findings": [
            {"id": "SCOPE", "statement": "范围明确", "evidence_refs": []}
        ],
    }


def run_normalizer(package: Path, source: Path, output: Path, role: str = "scope-routing"):
    return subprocess.run(
        [
            sys.executable,
            str(NORMALIZER),
            "--task-package",
            str(package.resolve()),
            "--role",
            role,
            "--source",
            str(source.resolve()),
            "--output",
            str(output.resolve()),
        ],
        capture_output=True,
        text=True,
    )


def test_wrong_correct_and_missing_declared_digest_normalize_identically(tmp_path: Path) -> None:
    package, work = create_package(tmp_path)
    unsigned = base_artifact()
    correct = sha256_bytes(canonical(unsigned))
    variants = [None, correct, "0" * 64]
    outputs: list[bytes] = []

    for index, declared in enumerate(variants):
        artifact = dict(unsigned)
        if declared is not None:
            artifact["artifact_sha256"] = declared
        source = work / f"scope-routing-{index}.raw.json"
        output = work / f"scope-routing-{index}.json"
        source.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
        source_before = source.read_bytes()

        result = run_normalizer(package, source, output)

        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "NORMALIZED"
        assert source.read_bytes() == source_before
        normalized = json.loads(output.read_text(encoding="utf-8"))
        assert normalized["artifact_sha256"] == correct
        outputs.append(output.read_bytes())

    assert outputs[0] == outputs[1] == outputs[2]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"unexpected": "field"}, "additional properties"),
        ({"task_id": "AUDIT-WRONG"}, "ARTIFACT_TASK_ID_MISMATCH"),
        ({"platform": "claude-code"}, "ARTIFACT_PLATFORM_MISMATCH"),
        ({"role": "static-audit"}, "ARTIFACT_ROLE_MISMATCH"),
    ],
)
def test_invalid_artifact_fields_fail_closed(
    tmp_path: Path, mutation: dict, reason: str
) -> None:
    package, work = create_package(tmp_path)
    artifact = base_artifact()
    artifact.update(mutation)
    source = work / "scope-routing.raw.json"
    output = work / "scope-routing.json"
    source.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")

    result = run_normalizer(package, source, output)

    assert result.returncode != 0
    assert reason in json.loads(result.stdout)["reason"]
    assert not output.exists()


def test_duplicate_key_preexisting_output_and_path_escape_fail_closed(tmp_path: Path) -> None:
    package, work = create_package(tmp_path)
    source = work / "scope-routing.raw.json"
    output = work / "scope-routing.json"
    valid = json.dumps(base_artifact(), ensure_ascii=False)
    source.write_text(valid[:-1] + ',"role":"scope-routing"}', encoding="utf-8")
    duplicate = run_normalizer(package, source, output)
    assert duplicate.returncode != 0
    assert "duplicate JSON key" in json.loads(duplicate.stdout)["reason"]

    source.write_text(valid, encoding="utf-8")
    output.write_text("occupied", encoding="utf-8")
    preexisting = run_normalizer(package, source, output)
    assert preexisting.returncode != 0
    assert json.loads(preexisting.stdout)["reason"] == "OUTPUT_ARTIFACT_ALREADY_EXISTS"

    outside = tmp_path / "outside.json"
    outside_result = run_normalizer(package, source, outside)
    assert outside_result.returncode != 0
    assert json.loads(outside_result.stdout)["reason"] == "OUTPUT_ARTIFACT_PATH_NOT_ALLOWED"


def test_task_package_drift_fails_closed(tmp_path: Path) -> None:
    package, work = create_package(tmp_path)
    data = json.loads(package.read_text(encoding="utf-8"))
    data["task_id"] = "AUDIT-DRIFTED"
    package.write_text(json.dumps(data), encoding="utf-8")
    source = work / "scope-routing.raw.json"
    output = work / "scope-routing.json"
    source.write_text(json.dumps(base_artifact()), encoding="utf-8")

    result = run_normalizer(package, source, output)

    assert result.returncode != 0
    assert json.loads(result.stdout)["reason"] == "TASK_PACKAGE_DIGEST_DRIFT"


def test_normalized_artifact_is_accepted_and_later_mutation_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "SKILL.md").write_text("---\nname: sample\n---\n", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for role in STATIC_ROLES:
        (prompts / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")
    output_root = tmp_path / "run"

    prepare = subprocess.run(
        [
            sys.executable,
            str(ENGINE),
            "prepare-run",
            "--task-id",
            "AUDIT-CODEX-NORMALIZER-TEST",
            "--platform",
            "codex",
            "--mode",
            "static",
            "--target",
            str(target),
            "--evidence-type",
            "skill",
            "--output-root",
            str(output_root),
            "--prompts-root",
            str(prompts),
        ],
        capture_output=True,
        text=True,
    )
    assert prepare.returncode == 0, prepare.stdout + prepare.stderr
    package = output_root / "task-package.json"
    work = output_root / "work"

    raw_artifact = base_artifact()
    raw_artifact["artifact_sha256"] = "0" * 64
    source = work / "scope-routing-artifact.raw.json"
    normalized = work / "scope-routing-artifact.json"
    source.write_text(json.dumps(raw_artifact, ensure_ascii=False), encoding="utf-8")
    normalize_result = run_normalizer(package, source, normalized)
    assert normalize_result.returncode == 0, normalize_result.stdout

    raw_record = work / "raw" / "scope-routing.jsonl"
    raw_record.parent.mkdir()
    raw_record.write_text('{"event":"completed"}\n', encoding="utf-8")
    receipt = work / "scope-routing-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "platform": "codex",
                "task_id": "AUDIT-CODEX-NORMALIZER-TEST",
                "role": "scope-routing",
                "kind": "codex-collaboration-receipt",
                "native_agent_type": "scope_routing",
                "invocation_id": "inv-scope-routing-001",
                "raw_record": {
                    "path": str(raw_record.resolve()),
                    "sha256": sha256_file(raw_record),
                },
                "completion": {"kind": "exit_status", "value": 0},
            }
        ),
        encoding="utf-8",
    )
    output_file = work / "scope-routing-output.md"
    output_file.write_text("scope complete\n", encoding="utf-8")
    outputs_file = work / "scope-routing-outputs.json"
    outputs_file.write_text(
        json.dumps(
            [{"path": str(output_file.resolve()), "sha256": sha256_file(output_file)}]
        ),
        encoding="utf-8",
    )

    write_result = subprocess.run(
        [
            sys.executable,
            str(ENGINE),
            "write-result",
            "--task-package",
            str(package),
            "--role",
            "scope-routing",
            "--status",
            "COMPLETED",
            "--receipt-file",
            str(receipt),
            "--artifact-file",
            str(normalized),
            "--outputs-file",
            str(outputs_file),
        ],
        capture_output=True,
        text=True,
    )
    assert write_result.returncode == 0, write_result.stdout + write_result.stderr
    assert json.loads(write_result.stdout)["status"] == "WRITTEN"

    tampered = json.loads(normalized.read_text(encoding="utf-8"))
    tampered["findings"][0]["statement"] = "tampered"
    normalized.write_text(json.dumps(tampered), encoding="utf-8")
    validate = subprocess.run(
        [
            sys.executable,
            str(ENGINE),
            "validate-result-set",
            "--task-package",
            str(package),
            "--results-dir",
            str(output_root / "agent-results"),
        ],
        capture_output=True,
        text=True,
    )
    assert validate.returncode != 0
    failure_codes = {failure["code"] for failure in json.loads(validate.stdout)["failures"]}
    assert "ARTIFACT_BINDING_DIGEST_MISMATCH" in failure_codes


def test_normalizer_is_packaged_only_for_codex() -> None:
    assert (GENERATED / "codex" / "skill" / "scripts" / NORMALIZER.name).is_file()
    for platform in ("claude-code", "kimi-code", "workbuddy"):
        assert not (GENERATED / platform / "skill" / "scripts" / NORMALIZER.name).exists()


def test_platform_builder_can_rebuild_same_output_with_codex_normalizer(
    tmp_path: Path,
) -> None:
    source_root = PACKAGE_ROOT
    candidate_a = tmp_path / "candidate-a"
    candidate_b = tmp_path / "candidate-b"
    for candidate in (candidate_a, candidate_b):
        foundation = candidate / "packages/skill-failure-auditor/plugin-src/core/foundation"
        foundation.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PACKAGE_ROOT / "plugin-src/core/foundation", foundation)
        migration = candidate / "packages/skill-failure-auditor/skill-family.migration.json"
        migration.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PACKAGE_ROOT / "skill-family.migration.json", migration)
    node = os.environ.get("SFA_FOUNDATION_NODE", "/opt/homebrew/Cellar/node@22/22.23.2/bin/node")
    first = subprocess.run(
        [sys.executable, str(BUILDER), "--source-package-root", str(source_root),
         "--candidate-root", str(candidate_a), "--node", node],
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    first_manifest = (candidate_a / "packages/skill-failure-auditor/generated/platforms/build-manifest.json").read_bytes()

    second = subprocess.run(
        [sys.executable, str(BUILDER), "--source-package-root", str(source_root),
         "--candidate-root", str(candidate_b), "--node", node],
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert (candidate_b / "packages/skill-failure-auditor/generated/platforms/build-manifest.json").read_bytes() == first_manifest
