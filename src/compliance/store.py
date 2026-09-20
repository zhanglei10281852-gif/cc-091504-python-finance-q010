"""事件存储。

系统以追加事件作为唯一事实来源，读取模型由事件重放得到。每个事件携带：

* ``business_time``：业务发生时间（由调用方给出，例如成交日、关系生效日）；
* ``received_at``：系统接收时间（存储层加盖，UTC）；
* ``version``：所属流（stream）内的单调版本号，用于乐观并发控制。

持久化为 JSONL，进程重启后整卷重放；``path=None`` 时退化为纯内存实现，
便于单元测试。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from compliance.errors import StateConflictError


class EventStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._versions: dict[str, int] = {}
        self._path: Path | None = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                self._events.append(event)
                stream = event["stream_id"]
                self._versions[stream] = max(
                    self._versions.get(stream, 0), event["version"]
                )

    def _persist(self, event: dict[str, Any]) -> None:
        if self._path is None:
            return
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    # ------------------------------------------------------------------ append/read

    def append(
        self,
        stream_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        expected_version: int | None = None,
        business_time: str | None = None,
    ) -> dict[str, Any]:
        """向指定流追加事件。

        ``expected_version`` 为调用方已读取到的该流最新版本号；传入时执行
        乐观并发检查（送审计划防并发覆盖依赖于此）。
        """

        with self._lock:
            current = self._versions.get(stream_id, 0)
            if expected_version is not None and expected_version != current:
                raise StateConflictError(
                    f"流 {stream_id} 已被其他修改更新（读取版本 {expected_version}，"
                    f"当前版本 {current}），请重新读取后再提交"
                )
            version = current + 1
            event = {
                "event_id": uuid.uuid4().hex,
                "stream_id": stream_id,
                "version": version,
                "event_type": event_type,
                "business_time": business_time,
                "received_at": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
            self._persist(event)
            self._events.append(event)
            self._versions[stream_id] = version
            return event

    def all_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def stream_events(self, stream_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["stream_id"] == stream_id]

    def stream_version(self, stream_id: str) -> int:
        with self._lock:
            return self._versions.get(stream_id, 0)

    def filter(self, event_types: Iterable[str]) -> list[dict[str, Any]]:
        wanted = set(event_types)
        with self._lock:
            return [e for e in self._events if e["event_type"] in wanted]
