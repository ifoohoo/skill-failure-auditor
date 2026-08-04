"""冻结高风险独立性规则的四类结论边界，防止提示词补丁反复摇摆。"""
from __future__ import annotations

import json
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = PACKAGE_ROOT / "spec" / "orchestration" / "semantic-claim-cases.json"
PROMPTS = PACKAGE_ROOT / "plugin-src" / "core" / "prompts"


def test_four_frozen_semantic_claim_cases_are_exact_and_coherent() -> None:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = {item["id"]: item for item in data["cases"]}
    assert set(cases) == {
        "local-test-only",
        "candidate-self-test-drives-formal-acceptance",
        "same-source-smoke-no-authoritative-claim",
        "external-adjudication-bound-to-evidence",
    }

    for case in cases.values():
        applies = case["drivesAuthoritativeState"]
        assert case["expectedApplicability"] == ("APPLIES" if applies else "NOT_APPLICABLE")
        if not applies:
            assert case["expectedDisposition"] == "PASS_WITHIN_FROZEN_SCOPE"
            assert case["mayClaimExternalGuarantee"] is False
        elif not case["externalIdentityEvidence"]:
            assert case["expectedDisposition"] == "BLOCKED"
            assert case["mayClaimExternalGuarantee"] is False
        else:
            assert case["expectedDisposition"] == "EVIDENCE_BOUND_REVIEW_REQUIRED"
            assert case["mayClaimExternalGuarantee"] is True


def test_prompts_preserve_execution_semantics_and_applicability_boundaries() -> None:
    evaluation = (PROMPTS / "evaluation-integrity.md").read_text(encoding="utf-8")
    adversarial = (PROMPTS / "adversarial-challenge.md").read_text(encoding="utf-8")
    synthesis = (PROMPTS / "result-synthesis.md").read_text(encoding="utf-8")

    assert "普通本地自测产生的 `PASS` 只表示该测试成功" in evaluation
    assert "只有目标作出这些声明" in evaluation
    assert "或该测试结果直接驱动这些状态时" in adversarial
    assert "前者不能冒充" in synthesis
    assert "也不能只因缺少外部身份而自动阻塞" in synthesis
