"""RED acceptance tests for the shared runtime/v1 workflow contract."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping

import pytest
import rfc8785


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
FIXTURES_ROOT = CONTRACTS_ROOT / "fixtures" / "runtime" / "v1"
SCHEMAS_ROOT = CONTRACTS_ROOT / "runtime" / "v1"
PACKAGED_SCHEMAS_ROOT = (
    REPOSITORY_ROOT / "Uni-Lab-OS" / "unilabos" / "workflow" / "schemas" / "runtime" / "v1"
)
REQUIRED_SCHEMA_NAMES = (
    "canonical-workflow.schema.json",
    "workflow-source-map.schema.json",
    "workflow-revision.schema.json",
    "workflow-change-proposal.schema.json",
)
HASH_PARITY_FIXTURE = "workflow-hash-parity.json"
SEMANTIC_PARITY_FIXTURE = "workflow-semantic-parity.json"


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES_ROOT / name).open(encoding="utf-8") as handle:
        value: object = json.load(handle)
    assert isinstance(value, dict)
    return value


def apply_json_pointer(
    document: dict[str, Any], pointer: str, value: Any
) -> dict[str, Any]:
    """Apply one test-owned JSON-pointer replacement to a cloned fixture."""

    if not pointer.startswith("/"):
        raise AssertionError(f"Expected JSON pointer, got {pointer!r}")
    target: Any = document
    parts = pointer.removeprefix("/").split("/")
    for part in parts[:-1]:
        key = part.replace("~1", "/").replace("~0", "~")
        target = target[int(key)] if isinstance(target, list) else target[key]
    final = parts[-1].replace("~1", "/").replace("~0", "~")
    if isinstance(target, list):
        target[int(final)] = value
    else:
        target[final] = value
    return document


def parity_revisions() -> list[tuple[str, dict[str, Any]]]:
    corpus = load_fixture(HASH_PARITY_FIXTURE)
    assert corpus.get("hash_algorithm") == "sha-256-jcs-rfc8785"
    cases = corpus.get("cases")
    assert isinstance(cases, list) and cases
    baseline = load_fixture("workflow-revision.json")
    revisions: list[tuple[str, dict[str, Any]]] = []
    for case in cases:
        assert isinstance(case, dict)
        name = case.get("name")
        replacement = case.get("replace")
        expected_hash = case.get("content_hash")
        assert isinstance(name, str) and name
        assert isinstance(replacement, dict)
        assert isinstance(replacement.get("pointer"), str)
        assert isinstance(expected_hash, str)
        revision = apply_json_pointer(
            copy.deepcopy(baseline), replacement["pointer"], replacement.get("value")
        )
        revision["content_hash"] = expected_hash
        revisions.append((name, revision))
    return revisions


def semantic_parity_cases(section: str) -> list[dict[str, Any]]:
    corpus = load_fixture(SEMANTIC_PARITY_FIXTURE)
    cases = corpus.get(section)
    assert isinstance(cases, list) and cases
    assert all(isinstance(case, dict) for case in cases)
    return cases


def jcs_hash_for_canonical(canonical_ir: Mapping[str, Any]) -> tuple[str, str]:
    """Apply the OS execution projection, then JCS UTF-8 SHA-256."""

    canonical_module = importlib.import_module("unilabos.workflow.canonical")
    revision = canonical_module.WorkflowRevision.model_validate(canonical_ir)
    payload = revision.model_dump(
        mode="json",
        exclude={"revision_id", "layout", "source_map", "source_artifact"},
        exclude_none=True,
    )
    for parameter in payload.get("parameters") or []:
        parameter.pop("title", None)
        parameter.pop("description", None)
    for invocation in payload.get("invocations") or []:
        invocation.pop("name", None)
        invocation.pop("description", None)
    canonical_module._normalize_execution_node_ids(payload)
    serialized = rfc8785.dumps(payload)
    return hashlib.sha256(serialized).hexdigest(), serialized.decode("utf-8")


def require_schema_authority() -> None:
    missing = [
        str(SCHEMAS_ROOT / name)
        for name in REQUIRED_SCHEMA_NAMES
        if not (SCHEMAS_ROOT / name).is_file()
    ]
    if missing:
        pytest.fail(
            "Missing production runtime/v1 schema authority: " + ", ".join(missing),
            pytrace=False,
        )
    for name in REQUIRED_SCHEMA_NAMES:
        with (SCHEMAS_ROOT / name).open(encoding="utf-8") as handle:
            schema: object = json.load(handle)
        assert isinstance(schema, dict), f"{name} must contain a JSON Schema object"
        assert "$schema" in schema
        encoded = json.dumps(schema)
        assert "ReactFlow" not in encoded
        assert "ast." not in encoded


def test_packaged_schema_mirror_matches_central_runtime_v1_authority() -> None:
    """C1: package schemas are a JSON-equivalent mirror, never a second authority."""

    require_schema_authority()
    for name in REQUIRED_SCHEMA_NAMES:
        central = SCHEMAS_ROOT / name
        packaged = PACKAGED_SCHEMAS_ROOT / name
        assert packaged.is_file(), f"Missing packaged runtime/v1 schema mirror: {packaged}"
        assert json.loads(packaged.read_text(encoding="utf-8")) == json.loads(
            central.read_text(encoding="utf-8")
        )


def require_contract_validators() -> tuple[Callable[..., object], Callable[..., object]]:
    try:
        contract = importlib.import_module("unilabos.workflow.contracts")
    except ModuleNotFoundError as error:
        if error.name != "unilabos.workflow.contracts":
            raise
        pytest.fail(
            "Missing Python runtime/v1 contract validator module: "
            "unilabos.workflow.contracts",
            pytrace=False,
        )
    revision = getattr(contract, "validate_workflow_revision", None)
    proposal = getattr(contract, "validate_workflow_change_proposal", None)
    if not callable(revision) or not callable(proposal):
        pytest.fail(
            "Missing public Python runtime/v1 validators: "
            "validate_workflow_revision and validate_workflow_change_proposal",
            pytrace=False,
        )
    return revision, proposal


def assert_invalid(call: Callable[[], object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        call()


def test_runtime_v1_schemas_are_the_structural_authority() -> None:
    require_schema_authority()


def test_valid_revision_uses_rfc8785_jcs_content_hash_and_shared_validator() -> None:
    require_schema_authority()
    validate_revision, _ = require_contract_validators()
    revision = load_fixture("workflow-revision.json")
    canonical = importlib.import_module("unilabos.workflow.canonical")

    assert canonical.WorkflowRevision.model_validate(
        revision["canonical_ir"]
    ).content_hash == revision["content_hash"]
    assert validate_revision(revision) is not None


def test_installed_wheel_validates_shared_revision_without_source_contracts(
    tmp_path: Path,
) -> None:
    """C1: a normal wheel must carry schemas beside its installed validator."""

    os_root = REPOSITORY_ROOT / "Uni-Lab-OS"
    wheel_directory = tmp_path / "wheel"
    installed_target = tmp_path / "installed"
    fixture_path = tmp_path / "workflow-revision.json"
    wheel_directory.mkdir()
    fixture_path.write_text(
        json.dumps(load_fixture("workflow-revision.json")), encoding="utf-8"
    )
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--wheel-dir",
            str(wheel_directory),
        ],
        capture_output=True,
        cwd=os_root,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(wheel_directory.glob("unilabos-*.whl"))
    assert len(wheels) == 1
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed_target),
            str(wheels[0]),
        ],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    child = """
import json
import os
import sys
from pathlib import Path
import unilabos.workflow.contracts as contracts
from unilabos.workflow.contracts import validate_workflow_revision

installed = Path(os.environ['C01_INSTALLED_TARGET']).resolve()
assert Path(contracts.__file__).resolve().is_relative_to(installed)
mirror = installed / 'unilabos' / 'workflow' / 'schemas' / 'runtime' / 'v1'
for name in ('canonical-workflow.schema.json', 'workflow-source-map.schema.json', 'workflow-revision.schema.json', 'workflow-change-proposal.schema.json'):
    assert (mirror / name).is_file(), f'missing packaged schema mirror: {mirror / name}'
assert not (installed / 'contracts' / 'runtime' / 'v1').exists()
payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
assert validate_workflow_revision(payload)['revision_id'] == 'revision-c01-valid'
"""
    environment = dict(os.environ)
    environment.pop("UNILAB_CONTRACTS_DIR", None)
    environment["C01_INSTALLED_TARGET"] = str(installed_target)
    environment["PYTHONPATH"] = str(installed_target)
    installed = subprocess.run(
        [sys.executable, "-c", child, str(fixture_path)],
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    assert installed.returncode == 0, (
        "Installed wheel must validate without UNILAB_CONTRACTS_DIR or source "
        f"checkout schemas. stderr:\n{installed.stderr}"
    )


def test_shared_hash_parity_corpus_uses_rfc8785_jcs_sha256() -> None:
    """C2: `content_hash` is RFC 8785/JCS UTF-8 SHA-256 in both languages."""

    require_schema_authority()
    validate_revision, _ = require_contract_validators()
    corpus = load_fixture(HASH_PARITY_FIXTURE)
    cases = corpus["cases"]
    assert isinstance(cases, list)

    names: list[str] = []
    validation_failures: list[str] = []
    for index, (name, revision) in enumerate(parity_revisions()):
        names.append(name)
        expected = cases[index]
        assert isinstance(expected, dict)
        digest, serialized = jcs_hash_for_canonical(revision["canonical_ir"])
        assert digest == revision["content_hash"]
        assert expected["jcs_fragment"] in serialized
        try:
            validate_revision(revision)
        except ValueError as error:
            validation_failures.append(f"{name}: {error}")
    assert names == [
        "literal-1.0",
        "literal-1e-7",
        "mixed-case-object-keys",
        "unicode-object-keys",
        "runtime-parameter-null-default",
        "rfc-number-sample",
        "negative-zero",
        "minimum-subnormal-double",
        "rfc-escaped-string-sample",
        "rfc-utf16-property-order",
    ]
    assert validation_failures == []


def test_os_rejects_lone_surrogate_in_rfc8785_canonical_literal() -> None:
    """I-R1: invalid Unicode terminates RFC 8785 canonicalization."""

    validate_revision, _ = require_contract_validators()
    case = semantic_parity_cases("rfc8785_invalid_cases")[0]
    revision = apply_json_pointer(
        load_fixture("workflow-revision.json"),
        case["replace"]["pointer"],
        case["replace"]["value"],
    )
    revision["content_hash"] = case["legacy_ts_content_hash"]
    canonical = importlib.import_module("unilabos.workflow.canonical")

    with pytest.raises(ValueError, match="outside the RFC 8785 JSON domain"):
        canonical.WorkflowRevision.model_validate(
            revision["canonical_ir"]
        ).content_hash
    with pytest.raises(ValueError, match="outside the RFC 8785 JSON domain"):
        validate_revision(revision)


@pytest.mark.parametrize(
    "case",
    semantic_parity_cases("parameter_cases"),
    ids=lambda case: case["name"],
)
def test_parameter_default_cases_match_os_canonical_authority(
    case: dict[str, Any],
) -> None:
    """I-R2: shared cases explicitly record OS acceptance or rejection."""

    validate_revision, _ = require_contract_validators()
    revision = load_fixture("workflow-revision.json")
    revision["canonical_ir"]["parameters"] = [case["parameter"]]
    revision["content_hash"] = case["content_hash"]
    canonical = importlib.import_module("unilabos.workflow.canonical")

    if case["os_accepts"]:
        model = canonical.WorkflowRevision.model_validate(revision["canonical_ir"])
        assert model.content_hash == revision["content_hash"]
        assert validate_revision(revision) is not None
        return

    with pytest.raises(ValueError, match="INVALID_WORKFLOW_PARAMETER_DEFAULT"):
        canonical.WorkflowRevision.model_validate(revision["canonical_ir"])
    with pytest.raises(ValueError, match="INVALID_WORKFLOW_PARAMETER_DEFAULT"):
        validate_revision(revision)


@pytest.mark.parametrize(
    "case",
    semantic_parity_cases("source_artifact_cases"),
    ids=lambda case: case["name"],
)
def test_source_artifact_cases_match_os_canonical_authority(
    case: dict[str, Any],
) -> None:
    """I-R2: URI and text-hash validity agree with the OS model."""

    validate_revision, _ = require_contract_validators()
    revision = load_fixture("workflow-revision.json")
    revision["canonical_ir"]["source_artifact"] = case["source_artifact"]
    canonical = importlib.import_module("unilabos.workflow.canonical")

    if case["os_accepts"]:
        canonical.WorkflowRevision.model_validate(revision["canonical_ir"])
        assert validate_revision(revision) is not None
        return

    with pytest.raises(ValueError):
        canonical.WorkflowRevision.model_validate(revision["canonical_ir"])
    with pytest.raises(ValueError):
        validate_revision(revision)


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("canonical content hash mismatch", lambda value: value.__setitem__("content_hash", "0" * 64)),
        ("source-map span references unknown node", lambda value: value["source_map"][0].__setitem__("node_id", "not-in-canonical-ir")),
        ("source-map span uses zero coordinates", lambda value: value["source_map"][0].__setitem__("start_line", 0)),
        ("source-map span end precedes start", lambda value: value["source_map"][0].update({"start_line": 7, "start_column": 8, "end_line": 6, "end_column": 1})),
        ("error diagnostic lacks a location", lambda value: value["diagnostics"].append({"severity": "error", "code": "COMPILE_ERROR", "message": "bad source"})),
    ],
)
def test_revision_validator_fails_closed_for_required_mutations(
    case: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    del case
    require_schema_authority()
    validate_revision, _ = require_contract_validators()
    revision = copy.deepcopy(load_fixture("workflow-revision.json"))
    mutate(revision)

    assert_invalid(lambda: validate_revision(revision))


def test_valid_proposal_uses_exactly_one_format_payload() -> None:
    require_schema_authority()
    _, validate_proposal = require_contract_validators()
    proposal = load_fixture("workflow-change-proposal.json")

    assert validate_proposal(proposal, current_revision_id="revision-c01-valid") is not None
    dual_payload = copy.deepcopy(proposal)
    dual_payload["ir_patch"] = []
    assert_invalid(lambda: validate_proposal(dual_payload, current_revision_id="revision-c01-valid"))


def test_proposal_validator_fails_closed_for_staleness_and_unsafe_fields() -> None:
    require_schema_authority()
    _, validate_proposal = require_contract_validators()
    proposal = load_fixture("workflow-change-proposal.json")

    assert_invalid(lambda: validate_proposal(proposal, current_revision_id="revision-c01-new"))
    for field in ("execution_request", "device_token", "database_credentials"):
        unsafe = copy.deepcopy(proposal)
        unsafe[field] = {"fixture": "must-not-be-accepted"}
        assert_invalid(
            lambda unsafe=unsafe: validate_proposal(
                unsafe, current_revision_id="revision-c01-valid"
            )
        )


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("model_metadata", "device_token"),
        ("model_metadata", "database_credentials"),
        ("ir_patch", "execution_request"),
        ("ir_patch", "device_token"),
    ],
)
def test_proposal_validator_rejects_nested_credentials_and_execution_requests(
    location: str, field: str
) -> None:
    """I2: proposal safety is recursive, not only top-level closure."""

    require_schema_authority()
    _, validate_proposal = require_contract_validators()
    proposal = load_fixture("workflow-change-proposal.json")
    unsafe = copy.deepcopy(proposal)
    if location == "model_metadata":
        unsafe["model_metadata"][field] = {"fixture": "must-not-be-accepted"}
    else:
        unsafe["format"] = "ir_patch"
        unsafe.pop("python_source")
        unsafe["ir_patch"] = [{field: {"fixture": "must-not-be-accepted"}}]
    assert_invalid(
        lambda: validate_proposal(unsafe, current_revision_id="revision-c01-valid")
    )


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("model_metadata", "deviceToken"),
        ("model_metadata", "databaseCredentials"),
        ("model_metadata", "executionRequest"),
        ("ir_patch", "executionRequest"),
    ],
)
def test_proposal_validator_rejects_camel_case_unsafe_field_variants(
    location: str, field: str
) -> None:
    """I-R3: separator normalization cannot bypass recursive safety."""

    _, validate_proposal = require_contract_validators()
    proposal = load_fixture("workflow-change-proposal.json")
    if location == "model_metadata":
        proposal["model_metadata"][field] = {"fixture": "must-not-be-accepted"}
    else:
        proposal["format"] = "ir_patch"
        proposal.pop("python_source")
        proposal["ir_patch"] = [{field: {"fixture": "must-not-be-accepted"}}]

    assert_invalid(
        lambda: validate_proposal(proposal, current_revision_id="revision-c01-valid")
    )


@pytest.mark.parametrize("created_at", ["not-a-date", "2026-99-99T99:99:99Z"])
def test_revision_validator_enforces_schema_datetime_format(created_at: str) -> None:
    """I3: Python must enforce the same date-time schema format as TypeScript."""

    require_schema_authority()
    validate_revision, _ = require_contract_validators()
    invalid = load_fixture("workflow-revision.json")
    invalid["created_at"] = created_at

    with pytest.raises(ValueError) as error:
        validate_revision(invalid)
    assert str(error.value) != "WORKFLOW_CONTENT_HASH_MISMATCH"
