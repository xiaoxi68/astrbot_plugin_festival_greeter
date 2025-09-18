from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path
import random
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

from .holidays import HolidayCalendar, HolidayOccurrence
from .message_builder import build_prompt, build_system_prompt, extract_text_from_response
from .state_store import DeliveryStateStore


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TRIGGER = time(hour=8, minute=0)
DEFAULT_PLATFORM = "aiocqhttp"
DEFAULT_MESSAGE_TYPE = "group"
DATA_SUBDIR = "festival_greeter"
PRUNE_INTERVAL = timedelta(days=7)
RETENTION_WINDOW = timedelta(days=400)


@register("festival-greeter", "xiaox", "节日问候推送插件", "0.1.0")
class FestivalGreetingPlugin(Star):
    def __init__(self, context: Context, config: Dict | None = None) -> None:
        super().__init__(context)
        self._config = dict(config or {})
        self._stop_event: Optional[asyncio.Event] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._prune_task: Optional[asyncio.Task] = None
        self._load_settings()
        self._state_store = DeliveryStateStore(self._resolve_state_path())
        self._sync_delivery_targets()

    def _load_settings(self) -> None:
        tz_name = str(self._config.get("timezone", DEFAULT_TIMEZONE)).strip() or DEFAULT_TIMEZONE
        try:
            self._timezone = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            logger.warning("未找到时区 %s，回退至 %s", tz_name, DEFAULT_TIMEZONE)
            self._timezone = ZoneInfo(DEFAULT_TIMEZONE)

        trigger_raw = str(self._config.get("trigger_time", "08:00")).strip()
        self._trigger_time = self._parse_trigger_time(trigger_raw)

        self._delivery_targets: List[str] = []

        mode = str(self._config.get("group_filter_mode", "disabled")).lower()
        if mode not in {"disabled", "whitelist", "blacklist"}:
            logger.warning("无效的群名单模式 %s，采用 disabled", mode)
            mode = "disabled"
        self._group_filter_mode = mode
        self._group_filter_entries = [str(item).strip() for item in self._config.get("group_filter_list", []) if str(item).strip()]

        repeat_mode = str(self._config.get("holiday_repeat_mode", "first-day")).strip().lower()
        if repeat_mode not in {"first-day", "every-day"}:
            logger.warning("无效的节日推送策略 %s，采用 first-day", repeat_mode)
            repeat_mode = "first-day"
        self._holiday_repeat_mode = repeat_mode

        self._llm_provider_id = str(self._config.get("llm_provider_id", "")).strip() or None
        self._llm_style = str(self._config.get("llm_prompt_style", "warm")).strip() or "warm"
        self._max_retries = max(0, int(self._config.get("max_generation_retries", 1)))
        self._allow_manual_trigger = bool(self._config.get("allow_manual_trigger", True))

        custom_defs = self._config.get("custom_holidays")
        self._calendar = HolidayCalendar.from_config(custom_defs)
        self._fallback_messages = [str(item).strip() for item in self._config.get("fallback_messages", []) if str(item).strip()]

    def _sync_delivery_targets(self) -> None:
        if not hasattr(self, "_state_store") or self._state_store is None:
            return
        if not hasattr(self._state_store, "list_groups"):
            return
        recorded_groups = self._state_store.list_groups()
        for group_id in recorded_groups:
            session = self._normalize_session(group_id)
            if session and session not in self._delivery_targets:
                self._delivery_targets.append(session)

    def _parse_trigger_time(self, raw: str) -> time:
        try:
            hour_str, minute_str = raw.split(":", 1)
            hour = max(0, min(23, int(hour_str)))
            minute = max(0, min(59, int(minute_str)))
            return time(hour=hour, minute=minute)
        except Exception:
            logger.warning("触发时间 %s 无效，使用默认 %s", raw, DEFAULT_TRIGGER.strftime("%H:%M"))
            return DEFAULT_TRIGGER

    async def initialize(self):
        self._stop_event = asyncio.Event()
        await self._state_store.prune_before(self._now() - RETENTION_WINDOW)
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        self._prune_task = asyncio.create_task(self._prune_loop())
        active_targets = self._apply_group_filter(self._delivery_targets)
        logger.info("节日祝福调度器已启动，目标群数量：%s", len(active_targets))

    async def terminate(self):
        if self._stop_event:
            self._stop_event.set()
        for task in (self._scheduler_task, self._prune_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._scheduler_task = None
        self._prune_task = None
        self._stop_event = None
        logger.info("节日祝福调度器已停止")

    async def _scheduler_loop(self) -> None:
        while self._stop_event and not self._stop_event.is_set():
            now = self._now()
            next_run = self._next_trigger(now)
            wait_seconds = max(0.0, (next_run - now).total_seconds())
            logger.debug("距离下次节日检查还有 %.1f 秒", wait_seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                break
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            await self._handle_tick(next_run)

    async def _prune_loop(self) -> None:
        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=PRUNE_INTERVAL.total_seconds())
                break
            except asyncio.TimeoutError:
                cutoff = self._now() - RETENTION_WINDOW
                await self._state_store.prune_before(cutoff)
            except asyncio.CancelledError:
                return

    def _next_trigger(self, now: datetime) -> datetime:
        candidate = datetime.combine(now.date(), self._trigger_time, tzinfo=self._timezone)
        if candidate <= now:
            candidate = datetime.combine(now.date() + timedelta(days=1), self._trigger_time, tzinfo=self._timezone)
        return candidate

    async def _handle_tick(self, scheduled_time: datetime) -> None:
        holidays = self._calendar.get_holidays_for(scheduled_time.date())
        if not holidays:
            logger.debug("%s 无节日，跳过推送", scheduled_time.date())
            return

        if self._holiday_repeat_mode == "first-day":
            holidays = [item for item in holidays if item.is_first_day]
            if not holidays:
                logger.debug("%s 非节日首日，配置为仅首日推送，跳过", scheduled_time.date())
                return

        sessions = self._apply_group_filter(self._delivery_targets)
        if not sessions:
            logger.warning("未配置可用群聊，节日祝福不会发送")
            return

        for holiday in holidays:
            await self._deliver_holiday(holiday, sessions)

    async def _deliver_holiday(self, holiday: HolidayOccurrence, sessions: Sequence[str]) -> None:
        for session in sessions:
            group_id = self._extract_group_id(session)
            now = self._now()
            if not await self._state_store.should_send(group_id, holiday.key, now, self._cooldown_window_hours()):
                continue
            message = await self._generate_message(holiday, group_id)
            if not message:
                logger.warning("节日 %s 生成祝福失败，跳过群 %s", holiday.definition.name, group_id)
                continue
            if await self._send_message(session, message):
                await self._state_store.mark_sent(group_id, holiday.key, now)

    async def _send_message(self, session: str, message: str) -> bool:
        chain = MessageChain().message(message)
        try:
            success = await self.context.send_message(session, chain)
            if not success:
                logger.warning("主动消息发送失败，session=%s", session)
            else:
                logger.info("已向 %s 发送节日祝福", session)
            return bool(success)
        except Exception as exc:
            logger.error("发送节日祝福失败: %s", exc)
            return False

    async def _generate_message(self, holiday: HolidayOccurrence, group_id: str) -> str:
        provider = self._resolve_provider()
        prompt_context = f"目标群 ID: {group_id}"
        prompt = build_prompt(holiday, self._llm_style, prompt_context)
        system_prompt = build_system_prompt(self._llm_style)
        last_error: Optional[Exception] = None

        if provider:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await provider.text_chat(
                        prompt=prompt,
                        context=[],
                        system_prompt=system_prompt,
                    )
                    text = extract_text_from_response(response)
                    if text:
                        return text
                except Exception as exc:  # pragma: no cover - 调用依赖外部环境
                    last_error = exc
                    logger.warning("调用 LLM 生成节日祝福失败 (attempt %s/%s): %s", attempt + 1, self._max_retries + 1, exc)
        else:
            logger.warning("未找到可用的 LLM Provider，使用兜底文案")

        if last_error:
            logger.error("节日祝福生成失败，使用兜底文案: %s", last_error)
        return self._build_fallback(holiday)

    def _resolve_provider(self):
        try:
            if self._llm_provider_id:
                provider = self.context.get_provider_by_id(self._llm_provider_id)
                if provider:
                    return provider
                logger.warning("未找到指定的 Provider %s", self._llm_provider_id)
            providers = self.context.get_all_providers()
            if providers:
                return providers[0]
        except (AttributeError, LookupError, RuntimeError) as exc:  # pragma: no cover
            logger.warning("获取 Provider 失败: %s", exc)
        return None

    def _build_fallback(self, holiday: HolidayOccurrence) -> str:
        if self._fallback_messages:
            template = random.choice(self._fallback_messages)
            return template.format(
                holiday=holiday.definition.name,
                date=holiday.current_date.strftime("%m月%d日"),
                year=holiday.current_date.year,
            )
        return (
            f"{holiday.definition.name}快乐！愿每位小伙伴都能与家人朋友共享温暖时光，"
            "心愿成真，未来可期。"
        )

    def _normalize_session(self, item) -> Optional[str]:
        text = str(item).strip()
        if not text:
            return None
        if ":" in text:
            return text
        return f"{DEFAULT_PLATFORM}:{DEFAULT_MESSAGE_TYPE}:{text}"

    def _apply_group_filter(self, sessions: Sequence[str]) -> List[str]:
        if self._group_filter_mode == "disabled":
            return list(sessions)
        filter_set = {self._normalize_filter_entry(item) for item in self._group_filter_entries}
        filter_set.discard("")
        result: List[str] = []
        if self._group_filter_mode == "whitelist":
            for session in sessions:
                gid = self._extract_group_id(session)
                if session in filter_set or gid in filter_set:
                    result.append(session)
        else:  # blacklist
            for session in sessions:
                gid = self._extract_group_id(session)
                if session in filter_set or gid in filter_set:
                    continue
                result.append(session)
        return result

    def _normalize_filter_entry(self, item: str) -> str:
        text = str(item).strip()
        if not text:
            return ""
        if ":" in text:
            return text
        return text

    def _extract_group_id(self, session: str) -> str:
        parts = session.split(":")
        return parts[-1] if parts else session

    def _resolve_state_path(self) -> Path:
        getters = []
        direct_getter = getattr(self.context, "get_data_dir", None)
        if callable(direct_getter):
            getters.append(direct_getter)
        tools = getattr(self.context, "tools", None)
        tool_getter = getattr(tools, "get_data_dir", None) if tools else None
        if callable(tool_getter):
            getters.append(tool_getter)

        for getter in getters:
            try:
                base = Path(getter())
                base.mkdir(parents=True, exist_ok=True)
                return base / "deliveries.json"
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("获取数据目录失败，尝试下一个候选：%s", exc)

        fallback_dir = Path("data") / DATA_SUBDIR
        fallback_dir.mkdir(parents=True, exist_ok=True)
        return fallback_dir / "deliveries.json"

    def _cooldown_window_hours(self) -> int:
        if self._holiday_repeat_mode == "every-day":
            return 0
        return 24

    def _now(self) -> datetime:
        return datetime.now(self._timezone)

    @filter.command("festival-send")
    async def manual_send(self, event: AstrMessageEvent):
        """手动触发当前节日祝福发送到所在会话。"""
        if not self._allow_manual_trigger:
            yield event.plain_result("插件未启用手动触发功能，请联系管理员修改配置。")
            return
        today = self._now().date()
        holidays = self._calendar.get_holidays_for(today)
        if not holidays:
            yield event.plain_result("今天没有配置的节日，稍后再试吧。")
            return
        session = event.unified_msg_origin
        sessions = self._apply_group_filter([session])
        if not sessions:
            yield event.plain_result("当前会话不在允许列表内，无法发送节日祝福。")
            return
        normalized_session = self._normalize_session(session)
        if normalized_session and normalized_session not in self._delivery_targets:
            self._delivery_targets.append(normalized_session)
        for holiday in holidays:
            gid = self._extract_group_id(session)
            now = self._now()
            if not await self._state_store.should_send(gid, holiday.key, now, self._cooldown_window_hours()):
                yield event.plain_result(f"{holiday.definition.name} 的祝福今天已经发送过啦。")
                continue
            message = await self._generate_message(holiday, gid)
            if not message:
                continue
            yield event.plain_result(message)
            await self._state_store.mark_sent(gid, holiday.key, now)

    @filter.command("festival-debug")
    async def debug_send(self, event: AstrMessageEvent):
        """管理员调试指令：忽略冷却，立即发送当日节日祝福。"""
        is_admin = False
        try:
            is_admin = bool(event.is_admin())
        except AttributeError:
            is_admin = getattr(event, "role", "member") == "admin"

        if not is_admin:
            yield event.plain_result("仅群管理员可以使用该调试指令。")
            return

        session = event.unified_msg_origin
        if not session:
            yield event.plain_result("无法识别当前会话，调试指令终止。")
            return

        today = self._now().date()
        holidays = self._calendar.get_holidays_for(today)
        if not holidays:
            yield event.plain_result("今天没有配置的节日，无需调试发送。")
            return

        normalized_session = self._normalize_session(session)
        if normalized_session and normalized_session not in self._delivery_targets:
            self._delivery_targets.append(normalized_session)

        target_session = normalized_session or session
        group_id = self._extract_group_id(target_session)

        successes = 0
        failures: List[str] = []

        for holiday in holidays:
            message = await self._generate_message(holiday, group_id)
            if not message:
                failures.append(holiday.definition.name)
                continue
            sent = await self._send_message(target_session, message)
            if sent:
                successes += 1
            else:
                failures.append(holiday.definition.name)

        if successes:
            detail = f"已向当前会话发送 {successes} 条节日祝福（调试模式未记录冷却）。"
            if failures:
                detail += " 未成功的节日：" + ", ".join(failures)
            yield event.plain_result(detail)
        else:
            if failures:
                yield event.plain_result("调试发送失败，相关节日：" + ", ".join(failures))
            else:
                yield event.plain_result("调试发送未产生任何祝福，请检查配置。")




