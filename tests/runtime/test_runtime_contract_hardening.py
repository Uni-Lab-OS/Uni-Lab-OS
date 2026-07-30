"""RED contracts for Canonical runtime identity and deep boundary validation."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.dag_model import DagValidationError, TaskDag
from unilabos.workflow.canonical import (
    ControlEdge,
    WorkflowRevision,
    WorkflowSourceArtifact,
)
from unilabos.workflow.dag_compile import compile_workflow_revision


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "station.measure": {
        "action_type": "UniLabJsonCommand",
        "inputs": {"reading": {"type": "number"}},
        "outputs": {"reading": {"type": "number"}},
        "contract": {
            "resource_claims": [
                {"resource_ref": "station/device", "mode": "exclusive"}
            ],
            "effects": [{"kind": "measurement_recorded"}],
            "timing": {"estimated_duration_s": 4.5},
        },
    }
}


class RecordingSchedule:
    def __init__(self) -> None:
        self.submitted: list[TaskDag] = []

    def on_job_status(self, _callback: Any) -> None:
        return None

    async def submit_dag(self, dag: TaskDag) -> Any:
        self.submitted.append(dag)
        return type("RunHandle", (), {"dag": dag})()

    def get_run(self, _task_id: str) -> None:
        return None


def _revision() -> WorkflowRevision:
    return WorkflowRevision.model_validate(
        {
            "schema_version": "2",
            "revision_id": "contract-hardening-1",
            "workflow_id": "contract-hardening",
            "invocations": [
                {
                    "node_id": "measure",
                    "action_ref": "station.measure",
                    # Registry contracts are authoritative; these values are stale.
                    "output_schema": {},
                    "resource_claims": [],
                    "effects": [],
                    "estimated_duration_s": 0,
                }
            ],
        }
    )


def _deeply_mutated_revision() -> WorkflowRevision:
    revision = _revision()
    revision.control_edges.append(
        ControlEdge(source="measure", target="missing-node")
    )
    return revision


def test_projected_canonical_revalidates_to_the_projected_execution_hash() -> None:
    schedule = RecordingSchedule()
    service = RuntimeService(schedule, action_catalog=ACTION_CATALOG)

    asyncio.run(
        service.start_run(
            {
                "source": {
                    "format": "canonical_workflow_v2",
                    "payload": _revision().model_dump(mode="json"),
                }
            }
        )
    )

    projection = service.get_workflow()["revision"]
    projected_revision = WorkflowRevision.model_validate(projection["canonical"])

    assert projected_revision.content_hash == projection["contentHash"]
    assert (
        projected_revision.content_hash
        == schedule.submitted[0].workflow_revision_hash
    )
    assert projected_revision.invocations[0].output_schema == {
        "reading": {"type": "number"}
    }
    assert schedule.submitted[0].nodes["measure"].action_type == (
        "UniLabJsonCommand"
    )


def test_compile_boundary_deeply_revalidates_a_mutated_frozen_revision() -> None:
    with pytest.raises(ValueError, match="unknown node"):
        compile_workflow_revision(
            _deeply_mutated_revision(),
            task_id="mutated-compile",
            action_catalog=ACTION_CATALOG,
        )


def test_compile_boundary_deeply_revalidates_mutated_binding_dict() -> None:
    revision = _revision()
    revision.invocations[0].input_bindings["reading"] = {  # type: ignore[assignment]
        "kind": "node_output",
        "node_id": "missing-node",
        "output": "reading",
    }

    with pytest.raises(ValueError, match="binding|unknown node"):
        compile_workflow_revision(
            revision,
            task_id="mutated-binding-compile",
            action_catalog=ACTION_CATALOG,
        )


def test_set_boundary_deeply_revalidates_a_mutated_frozen_revision() -> None:
    service = RuntimeService(RecordingSchedule(), action_catalog=ACTION_CATALOG)

    with pytest.raises(ValueError, match="unknown node"):
        service.set_workflow_revision(_deeply_mutated_revision())


class MutatingProfile:
    action_catalog = ACTION_CATALOG

    def import_workflow_source(self, *_args: Any, **_kwargs: Any) -> WorkflowRevision:
        return _deeply_mutated_revision()


class RecordingJournal:
    def __init__(self) -> None:
        self.persisted = False

    def record_run_submission(self, **_kwargs: Any) -> None:
        self.persisted = True


def test_persist_boundary_rejects_mutated_profile_output_before_journaling() -> None:
    journal = RecordingJournal()
    service = RuntimeService(
        RecordingSchedule(),
        action_catalog=ACTION_CATALOG,
        profiles={"mutating-profile": MutatingProfile()},  # type: ignore[dict-item]
        journal=journal,  # type: ignore[arg-type]
    )

    with pytest.raises(DagValidationError, match="unknown node"):
        asyncio.run(
            service.start_run(
                {
                    "profile_ref": "mutating-profile",
                    "source": {
                        "format": "profile_workflow",
                        "payload": {"schema": "test/v1", "kind": "operation"},
                    },
                }
            )
        )

    assert journal.persisted is False


def _git_blob_hash(text: str) -> str:
    payload = text.encode("utf-8")
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def _sha256_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


@pytest.mark.parametrize("hash_kind", ["git-blob", "sha256"])
def test_source_artifact_accepts_a_matching_supported_content_hash(
    hash_kind: str,
) -> None:
    text = "schema: example.workflow/v1\nname: secure-source\n"
    content_hash = (
        _git_blob_hash(text) if hash_kind == "git-blob" else _sha256_hash(text)
    )

    artifact = WorkflowSourceArtifact(
        format="example.workflow/v1",
        text=text,
        uri="workflows/secure-source.yaml",
        content_hash=content_hash,
    )

    assert artifact.content_hash == content_hash


@pytest.mark.parametrize(
    "unsafe_uri",
    [
        "/etc/passwd",
        "../outside.yaml",
        "workflows/../../outside.yaml",
        "file:///tmp/workflow.yaml",
        "C:\\temp\\workflow.yaml",
    ],
)
def test_source_artifact_rejects_non_relative_or_traversing_uri(
    unsafe_uri: str,
) -> None:
    text = "schema: example.workflow/v1\n"

    with pytest.raises(ValueError, match="uri|relative|path|traversal"):
        WorkflowSourceArtifact(
            format="example.workflow/v1",
            text=text,
            uri=unsafe_uri,
            content_hash=_git_blob_hash(text),
        )


@pytest.mark.parametrize(
    "unsupported_hash",
    [
        "git-blob-001",
        "md5:d41d8cd98f00b204e9800998ecf8427e",
        "not-a-content-hash",
    ],
)
def test_source_artifact_rejects_unsupported_hash_format(
    unsupported_hash: str,
) -> None:
    with pytest.raises(ValueError, match="hash|sha"):
        WorkflowSourceArtifact(
            format="example.workflow/v1",
            text="schema: example.workflow/v1\n",
            uri="workflows/source.yaml",
            content_hash=unsupported_hash,
        )


def test_source_artifact_rejects_content_hash_that_does_not_match_text() -> None:
    with pytest.raises(ValueError, match="hash|match|content"):
        WorkflowSourceArtifact(
            format="example.workflow/v1",
            text="schema: example.workflow/v1\n",
            uri="workflows/source.yaml",
            content_hash="0" * 40,
        )


def test_source_artifact_rejects_text_larger_than_two_mebibytes() -> None:
    text = "x" * (2 * 1024 * 1024 + 1)

    with pytest.raises(ValueError, match="text|length|size|character"):
        WorkflowSourceArtifact(
            format="example.workflow/v1",
            text=text,
            uri="workflows/source.yaml",
            content_hash=_sha256_hash(text),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "f" * 129),
        ("uri", f"workflows/{'u' * 1015}"),
    ],
)
def test_source_artifact_rejects_oversized_metadata_fields(
    field: str,
    value: str,
) -> None:
    text = "schema: example.workflow/v1\n"
    payload = {
        "format": "example.workflow/v1",
        "text": text,
        "uri": "workflows/source.yaml",
        "content_hash": _git_blob_hash(text),
    }
    payload[field] = value

    with pytest.raises(ValueError, match=f"{field}|length|size|character"):
        WorkflowSourceArtifact.model_validate(payload)
