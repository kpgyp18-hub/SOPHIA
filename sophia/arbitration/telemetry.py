from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sophia.arbitration.graph import ArbitrationResult


class ArbitrationTelemetry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(self, task_id: str, result: ArbitrationResult, *, actual_route: str | None = None,
               operation: str = "shadow") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        winners = set(result.winners)
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.path.open("a", encoding="utf-8") as handle:
            for node_id, activation in result.activations.items():
                handle.write(json.dumps({
                    "task_id": task_id, "mode": result.mode.value, "operation": operation,
                    "node_id": node_id, "activation": activation, "winner": node_id in winners,
                    "trace": result.trace.get(node_id, ()), "actual_route": actual_route,
                    "timestamp": timestamp,
                }, ensure_ascii=False) + "\n")
