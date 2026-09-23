"""Read-only runtime health aggregation for the v0.5 control plane."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from sophia.brains.ollama import BrainRegistry
from sophia.database.manager import DatabaseManager
from sophia.runtime.tool_registry import ToolRegistry
from sophia.workers import WorkerPool


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str


class HealthManager:
    def __init__(self, registry: BrainRegistry, database: DatabaseManager, workspace: str | Path,
                 tools: ToolRegistry | None = None, workers: WorkerPool | None = None) -> None:
        self.registry, self.database, self.workspace = registry, database, Path(workspace)
        self.tools, self.workers = tools, workers

    def checks(self) -> list[HealthCheck]:
        checks = [
            HealthCheck("core", True, "ready"),
            self._workspace_check(),
            HealthCheck("brains", bool(self.registry.names()), ", ".join(self.registry.names()) or "none configured"),
        ]
        checks.extend((self._ollama_check(), self._gpu_check(), self._ram_check()))
        if self.tools is not None:
            checks.append(HealthCheck("tools", bool(self.tools.names()), ", ".join(self.tools.names()) or "none registered"))
        if self.workers is not None:
            checks.append(HealthCheck("workers", self.workers.max_worker_count > 0,
                                      f"capacity={self.workers.max_worker_count}, depth={self.workers.max_worker_depth}"))
        for name, health in self.database.health().items():
            detail = health.detail + (" pgvector=OK" if health.vector_ready else "")
            checks.append(HealthCheck(name, health.ok, detail))
        return checks

    def _workspace_check(self) -> HealthCheck:
        usable = self.workspace.is_dir() and os.access(self.workspace, os.R_OK | os.W_OK)
        return HealthCheck("workspace", usable, str(self.workspace))

    def _ollama_check(self) -> HealthCheck:
        """Verify the local daemon without causing a model load or generation."""
        endpoint = next(
            (str(getattr(self.registry.get(name), "endpoint"))
             for name in self.registry.names()
             if getattr(self.registry.get(name), "endpoint", None)),
            None,
        )
        if endpoint is None:
            return HealthCheck("ollama", False, "no Ollama brain configured")
        try:
            with urlopen(f"{endpoint.rstrip('/')}/api/tags", timeout=3) as response:
                payload = json.load(response)
            models = payload.get("models", [])
            return HealthCheck("ollama", True, f"reachable models={len(models)}")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return HealthCheck("ollama", False, str(error))

    def _gpu_check(self) -> HealthCheck:
        """Report GPU capability using the vendor CLI when it is available."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=3, check=True,
            )
            line = next((item.strip() for item in result.stdout.splitlines() if item.strip()), "")
            return HealthCheck("gpu", bool(line), line or "no NVIDIA GPU reported")
        except (OSError, subprocess.SubprocessError) as error:
            return HealthCheck("gpu", False, f"unavailable: {error}")

    def _ram_check(self) -> HealthCheck:
        total = total_physical_memory_bytes()
        if total is None:
            return HealthCheck("ram", False, "physical memory unavailable")
        return HealthCheck("ram", True, f"{total / (1024 ** 3):.1f} GiB total")

    def status_lines(self) -> list[str]:
        return [f"{check.name.upper():<10} {'OK' if check.ok else 'DOWN'} {check.detail}" for check in self.checks()]


def total_physical_memory_bytes() -> int | None:
    """Use a native Windows query, with a POSIX fallback for development hosts."""
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
        return None
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return None
