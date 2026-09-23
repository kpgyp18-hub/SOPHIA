"""Local interfaces for vision, voice, and hardware milestones.

They are intentionally adapters/simulators: hardware and cloud services are never
contacted merely because a task was routed here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat


class VisionAdapter:
    """Offline image inspection with a durable, inspectable result artifact."""

    def inspect(self, image: str | Path) -> dict[str, Any]:
        path = Path(image)
        if not path.is_file():
            return {"path": str(path), "status": "missing"}
        try:
            with Image.open(path) as source:
                source.load()
                rgb = source.convert("RGB")
                stat = ImageStat.Stat(rgb)
                return {
                    "path": str(path.resolve()),
                    "status": "analyzed",
                    "format": source.format,
                    "mode": source.mode,
                    "width": source.width,
                    "height": source.height,
                    "mean_rgb": [round(value, 2) for value in stat.mean],
                }
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            return {"path": str(path.resolve()), "status": "failed", "error": str(error)}

    def inspect_to_file(self, image: str | Path, output: str | Path) -> dict[str, Any]:
        result = self.inspect(image)
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


class VoiceAdapter:
    def transcribe(self, audio: str | Path) -> str:
        return f"[STT adapter pending: {Path(audio).name}]"

    def synthesize(self, text: str) -> bytes:
        return text.encode("utf-8")


@dataclass
class FloatingUnit:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class HardwareGateway:
    """ROS2-ready boundary with a deterministic simulator as the safe default."""
    def __init__(self) -> None:
        self.unit = FloatingUnit()

    def move(self, x: float, y: float, z: float) -> FloatingUnit:
        self.unit = FloatingUnit(x, y, z)
        return self.unit
