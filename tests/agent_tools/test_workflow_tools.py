from __future__ import annotations

from unilabos.agent_tools.workflow import WorkflowAgentTools


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def create_task(self, workflow_uuid, **options):
        self.calls.append(("run", (workflow_uuid, options)))
        return {"taskUuid": "task-1", "sourceIdentity": {"authority": "backend"}}

    def wait_authoring(self, workflow_uuid, **options):
        self.calls.append(("authoring", (workflow_uuid, options)))
        return {"revision": 8, "frontendRefresh": "authoring-sse"}


def test_agent_tools_delegate_to_shared_domain_client(monkeypatch, tmp_path) -> None:
    client = RecordingClient()
    tools = WorkflowAgentTools(tmp_path)
    monkeypatch.setattr(tools, "_client", lambda: client)

    run = tools.run_workflow(
        "workflow-1",
        run_mode="single_node",
        target_node_uuid="node-1",
        operation_id="operation-1",
    )
    authoring = tools.wait_authoring(
        "workflow-1", after_revision=7, timeout=2
    )

    assert run["sourceIdentity"]["authority"] == "backend"
    assert authoring["revision"] == 8
    assert client.calls == [
        (
            "run",
            (
                "workflow-1",
                {
                    "run_mode": "single_node",
                    "target_node_uuid": "node-1",
                    "input_value": None,
                    "operation_id": "operation-1",
                },
            ),
        ),
        (
            "authoring",
            ("workflow-1", {"after_revision": 7, "timeout": 2}),
        ),
    ]
