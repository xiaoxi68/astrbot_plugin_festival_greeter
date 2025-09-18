"""节日规则与查询工具。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import logging
import re
from typing import Iterable, List, Mapping, Sequence


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HolidayDefinition:
    """用于描述公历或预置节日的不可变配置。"""

    name: str
    month: int
    day: int
    length_days: int = 1
    aliases: Sequence[str] = field(default_factory=tuple)
    description: str = ""
    dynamic_dates: Mapping[int, tuple[int, int]] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", self.name.lower())
        return base.strip("-") or "holiday"

    @property
    def duration_days(self) -> int:
        return max(1, int(self.length_days))

    def start_date_for_year(self, year: int) -> date | None:
        month, day = self.month, self.day
        if self.dynamic_dates:
            override = self.dynamic_dates.get(year)
            if override:
                month, day = override
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def matches_date(self, target: date) -> bool:
        start = self.start_date_for_year(target.year)
        if not start:
            return False
        delta = (target - start).days
        return 0 <= delta < self.duration_days

    def occurrence_on(self, target: date) -> "HolidayOccurrence | None":
        start = self.start_date_for_year(target.year)
        if not start:
            return None
        delta = (target - start).days
        if 0 <= delta < self.duration_days:
            return HolidayOccurrence(self, target, start)
        return None


@dataclass(frozen=True)
class HolidayOccurrence:
    definition: HolidayDefinition
    current_date: date
    start_date: date

    @property
    def key(self) -> str:
        return f"{self.definition.slug}-{self.current_date.isoformat()}"

    @property
    def day_offset(self) -> int:
        return (self.current_date - self.start_date).days

    @property
    def is_first_day(self) -> bool:
        return self.day_offset == 0

    def to_payload(self) -> dict:
        return {
            "name": self.definition.name,
            "date": self.current_date.isoformat(),
            "start_date": self.start_date.isoformat(),
            "length_days": self.definition.duration_days,
            "day_offset": self.day_offset,
            "is_first_day": self.is_first_day,
            "aliases": list(self.definition.aliases),
            "description": self.definition.description,
            "slug": self.definition.slug,
        }

DEFAULT_HOLIDAYS: List[HolidayDefinition] = [
    HolidayDefinition("元旦", 1, 1, aliases=("新年",)),
    HolidayDefinition("春节", 2, 1, length_days=7, aliases=("新春", "Spring Festival"), dynamic_dates={
        2024: (2, 10),
        2025: (1, 29),
        2026: (2, 17),
        2027: (2, 6),
        2028: (1, 26),
        2029: (2, 13),
        2030: (2, 3),
    }),
    HolidayDefinition("元宵节", 2, 15, aliases=("上元节",), dynamic_dates={
        2024: (2, 24),
        2025: (2, 12),
        2026: (3, 3),
        2027: (2, 20),
        2028: (2, 9),
        2029: (2, 28),
        2030: (2, 18),
    }),
    HolidayDefinition("妇女节", 3, 8, aliases=("女神节",)),
    HolidayDefinition("植树节", 3, 12),
    HolidayDefinition("清明节", 4, 5, aliases=("踏青节",)),
    HolidayDefinition("劳动节", 5, 1, length_days=3, aliases=("五一",)),
    HolidayDefinition("青年节", 5, 4),
    HolidayDefinition("端午节", 6, 3, aliases=("龙舟节",), dynamic_dates={
        2024: (6, 10),
        2025: (5, 31),
        2026: (6, 19),
        2027: (6, 9),
        2028: (5, 28),
        2029: (6, 16),
        2030: (6, 5),
    }),
    HolidayDefinition("儿童节", 6, 1, aliases=("六一",)),
    HolidayDefinition("建党节", 7, 1),
    HolidayDefinition("七夕节", 8, 7, aliases=("乞巧节",), dynamic_dates={
        2024: (8, 10),
        2025: (8, 29),
        2026: (8, 19),
        2027: (8, 8),
        2028: (8, 26),
        2029: (8, 15),
        2030: (8, 4),
    }),
    HolidayDefinition("建军节", 8, 1),
    HolidayDefinition("教师节", 9, 10),
    HolidayDefinition("中秋节", 9, 15, length_days=3, aliases=("月圆节", "Moon Festival"), dynamic_dates={
        2024: (9, 17),
        2025: (10, 6),
        2026: (9, 25),
        2027: (9, 15),
        2028: (10, 3),
        2029: (9, 22),
        2030: (9, 12),
    }),
    HolidayDefinition("国庆节", 10, 1, length_days=7, aliases=("十一", "National Day")),
    HolidayDefinition("重阳节", 10, 9, aliases=("敬老节",), dynamic_dates={
        2024: (10, 11),
        2025: (10, 29),
        2026: (10, 18),
        2027: (10, 7),
        2028: (10, 26),
        2029: (10, 15),
        2030: (10, 4),
    }),
]


class HolidayCalendar:
    """节日查询服务，可合并自定义节日。"""

    def __init__(self, custom_definitions: Iterable[HolidayDefinition] | None = None) -> None:
        self._definitions: List[HolidayDefinition] = list(DEFAULT_HOLIDAYS)
        if custom_definitions:
            for item in custom_definitions:
                if not isinstance(item, HolidayDefinition):
                    continue
                # 若自定义节日与默认同名，覆盖默认定义。
                self._definitions = [
                    d for d in self._definitions if d.slug != item.slug
                ]
                self._definitions.append(item)

    @staticmethod
    def from_config(items: Sequence | None) -> "HolidayCalendar":
        definitions: List[HolidayDefinition] = []
        if not items:
            return HolidayCalendar(definitions)

        if any(isinstance(raw, dict) for raw in items):
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                try:
                    definitions.append(
                        HolidayDefinition(
                            name=str(raw.get("name", "")).strip() or "未命名节日",
                            month=int(raw.get("month")),
                            day=int(raw.get("day")),
                            length_days=max(1, int(raw.get("length_days", 1) or 1)),
                            aliases=tuple(
                                str(alias).strip()
                                for alias in raw.get("aliases", [])
                                if str(alias).strip()
                            ),
                            description=str(raw.get("description", "")),
                        )
                    )
                except Exception as exc:
                    logger.warning("自定义节日配置无效: %s", exc)
            return HolidayCalendar(definitions)

        buffer: List[str] = []
        for raw in items:
            if not isinstance(raw, str):
                continue
            text = raw.strip()
            if not text:
                continue
            buffer.append(text)
            if len(buffer) < 2:
                continue
            date_token, name = buffer
            buffer = []
            if not DATE_TOKEN_PATTERN.fullmatch(date_token):
                logger.warning("忽略无效节日日期标记: %s", date_token)
                continue
            name = name.strip()
            if not name:
                logger.warning("节日名称为空，已跳过日期 %s", date_token)
                continue
            try:
                month = int(date_token[:2])
                day = int(date_token[2:])
                definitions.append(
                    HolidayDefinition(
                        name=name,
                        month=month,
                        day=day,
                    )
                )
            except ValueError as exc:
                logger.warning("解析节日日期失败: %s", exc)

        if buffer:
            logger.warning("自定义节日配置存在未配对的条目，已忽略: %s", buffer[0])

        return HolidayCalendar(definitions)

    def get_holidays_for(self, target_date: date) -> List[HolidayOccurrence]:
        results: List[HolidayOccurrence] = []
        for definition in self._definitions:
            occurrence = definition.occurrence_on(target_date)
            if occurrence:
                results.append(occurrence)
        return results

    def list_all(self) -> List[HolidayDefinition]:
        return list(self._definitions)
DATE_TOKEN_PATTERN = re.compile(r"^(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$")
