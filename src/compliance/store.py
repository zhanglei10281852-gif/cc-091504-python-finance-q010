"""追加式事件存储：所有状态变化先写事件日志，再应用到内存状态。

持久化文件为 `.runtime/events.jsonl`（每行一个 JSON 事件）。服务重启时
按 seq 顺序重放全部事件重建状态；累计额度等派生数据不落地，始终由
成交事件重放计算，保证"可重放、不重复扣减"。
"""
from __future__ import annotations

import json
from pathlib import Path


class EventStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.events: list[dict] = []
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self.events.append(json.loads(line))

    def append(self, event: dict) -> None:
        self.events.append(event)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
