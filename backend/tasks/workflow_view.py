"""Read-only rendering of historical workflow records; new tasks use RunCoordinator."""
from typing import Any


def build_graph_view(
    *,
    workflow_id: str,
    stages: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    orchestration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    orchestration = orchestration or {}
    current_stage_key = orchestration.get("currentStageKey")
    nodes = [
        {
            "id": f"stage:{stage['key']}",
            "kind": "stage",
            "label": stage["title"],
            "stageKey": stage["key"],
            "status": stage["status"],
            "active": stage["key"] == current_stage_key,
            "position": stage.get("position"),
        }
        for stage in stages
    ]
    nodes.extend(
        {
            "id": f"assignment:{assignment['id']}",
            "kind": "assignment",
            "label": assignment["summary"],
            "stageId": assignment.get("stageId"),
            "agentId": assignment["agentId"],
            "status": assignment["status"],
            "skillLabel": assignment.get("skillLabel"),
            "allowParallel": assignment.get("allowParallel", True),
        }
        for assignment in assignments
    )

    edges = []
    ordered_stage_ids = [f"stage:{stage['key']}" for stage in stages]
    for index in range(len(ordered_stage_ids) - 1):
        edges.append(
            {
                "id": f"edge:{ordered_stage_ids[index]}->{ordered_stage_ids[index + 1]}",
                "from": ordered_stage_ids[index],
                "to": ordered_stage_ids[index + 1],
                "kind": "stage-flow",
            }
        )

    stage_key_by_id = {stage["id"]: stage["key"] for stage in stages}
    for assignment in assignments:
        stage_key = stage_key_by_id.get(assignment.get("stageId"))
        if stage_key:
            edges.append(
                {
                    "id": f"edge:stage:{stage_key}->assignment:{assignment['id']}",
                    "from": f"stage:{stage_key}",
                    "to": f"assignment:{assignment['id']}",
                    "kind": "assignment-branch",
                }
            )

    return {
        "workflowId": workflow_id,
        "engine": orchestration.get("engine") or "builtin",
        "version": orchestration.get("graphVersion") or "v2",
        "currentStageKey": current_stage_key,
        "workflowStatus": orchestration.get("workflowStatus"),
        "nodes": nodes,
        "edges": edges,
    }
