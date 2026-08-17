// Fixed dispatch: every registered schema $id mapped to its generated
// standalone validator function.
import * as validate202012 from "./validate-2020-12.mjs";

const STANDALONE_VALIDATORS = Object.freeze({
  "attempt-manifest.schema.json": validate202012.__skillFamilyFoundationValidator_00000,
  "audit-result.schema.json": validate202012.__skillFamilyFoundationValidator_00001,
  "continuation-package.schema.json": validate202012.__skillFamilyFoundationValidator_00002,
  "failure-mode.schema.json": validate202012.__skillFamilyFoundationValidator_00003,
  "https://contracts.skill-family.example/candidate/quickstart-profile/v2/consumer-schema-inventory.json": validate202012.__skillFamilyFoundationValidator_00004,
  "https://contracts.skill-family.example/candidate/quickstart-profile/v2/harness-surface-detectors.json": validate202012.__skillFamilyFoundationValidator_00005,
  "https://contracts.skill-family.example/candidate/quickstart-profile/v2/harness-surface-inventory.json": validate202012.__skillFamilyFoundationValidator_00006,
  "https://contracts.skill-family.example/candidate/quickstart-profile/v2/resource.json": validate202012.__skillFamilyFoundationValidator_00007,
  "https://contracts.skill-family.example/candidate/quickstart-profile/v2/result.json": validate202012.__skillFamilyFoundationValidator_00008,
  "https://contracts.skill-family.example/candidate/quickstart-profile/v2/task.json": validate202012.__skillFamilyFoundationValidator_00009,
  "https://contracts.skill-family.example/v1/migration-manifest.json": validate202012.__skillFamilyFoundationValidator_00010,
  "https://contracts.skill-family.example/v1/operation-request.json": validate202012.__skillFamilyFoundationValidator_00011,
  "https://contracts.skill-family.example/v1/operation-result.json": validate202012.__skillFamilyFoundationValidator_00012,
  "skill-failure-auditor:orchestration:result:2.1.0": validate202012.__skillFamilyFoundationValidator_00013,
  "skill-failure-auditor:orchestration:role-artifact:1.1.0": validate202012.__skillFamilyFoundationValidator_00014,
  "skill-failure-auditor:orchestration:task-package:2.1.0": validate202012.__skillFamilyFoundationValidator_00015,
  "source-manifest.schema.json": validate202012.__skillFamilyFoundationValidator_00016,
  "urn:loop-agent:schema:delivery-task-result": validate202012.__skillFamilyFoundationValidator_00017,
});

export default STANDALONE_VALIDATORS;
