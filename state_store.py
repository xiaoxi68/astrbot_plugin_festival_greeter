"""节日祝福发送状态存储。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DeliveryStateStore:
    """对节日祝福发送记录进行持久化，避免重复推送。"""

    def __init__(self, storage_path: Path) -> None:
        self._path = storage_path
        self._lock = asyncio.Lock()
        self._state: Dict[str, Dict[str, str]] = {"deliveries": {}}
        self._ensure_parent()
        self._load()

    def _ensure_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
            deliveries = raw.get("deliveries")
            if isinstance(deliveries, dict):
                normalized: Dict[str, Dict[str, str]] = {}
                for group, records in deliveries.items():
                    if not isinstance(records, dict):
                        continue
                    normalized[str(group)] = {
                        str(key): str(timestamp)
                        for key, timestamp in records.items()
                    }
                self._state["deliveries"] = normalized
        except Exception as exc:  # pragma: no cover - IO 容错
            logger.warning("加载节日发送记录失败: %s", exc)

    def _flush(self) -> None:
        data = json.dumps(self._state, ensure_ascii=False, indent=2)
        self._path.write_text(data, encoding="utf-8")

    async def get_last_sent(self, group_id: str, holiday_key: str) -> Optional[datetime]:
        async with self._lock:
            records = self._state["deliveries"].get(str(group_id), {})
            value = records.get(holiday_key)
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    async def should_send(self, group_id: str, holiday_key: str, now: datetime, cooldown_hours: int) -> bool:
        last_sent = await self.get_last_sent(group_id, holiday_key)
        if not last_sent:
            return True
        if cooldown_hours <= 0:
            return last_sent.date() != now.date()
        return now - last_sent >= timedelta(hours=cooldown_hours)

    async def mark_sent(self, group_id: str, holiday_key: str, timestamp: datetime) -> None:
        async with self._lock:
            deliveries = self._state.setdefault("deliveries", {})
            group_records = deliveries.setdefault(str(group_id), {})
            group_records[holiday_key] = timestamp.isoformat()
            try:
                self._flush()
            except Exception as exc:  # pragma: no cover
                logger.warning("写入节日发送记录失败: %s", exc)

    def list_groups(self) -> List[str]:
        return list(self._state.get("deliveries", {}).keys())

    async def prune_before(self, cutoff: datetime) -> None:
        async with self._lock:
            deliveries = self._state.setdefault("deliveries", {})
            for group_id, records in list(deliveries.items()):
                new_records = {
                    key: ts
                    for key, ts in records.items()
                    if self._is_recent(ts, cutoff)
                }
                if new_records:
                    deliveries[group_id] = new_records
                else:
                    deliveries.pop(group_id, None)
            try:
                self._flush()
            except Exception as exc:  # pragma: no cover
                logger.warning("清理节日发送记录失败: %s", exc)

    @staticmethod
    def _is_recent(timestamp: str, cutoff: datetime) -> bool:
        try:
            dt = datetime.fromisoformat(timestamp)
        except ValueError:
            return False
        return dt >= cutoff
