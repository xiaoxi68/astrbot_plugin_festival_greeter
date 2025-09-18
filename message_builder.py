"""祝福语生成辅助函数。"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable

from .holidays import HolidayOccurrence

STYLE_GUIDANCE: Dict[str, str] = {
    "warm": "语气温暖、真诚，适合多数正式或半正式群聊。",
    "formal": "语气端庄，适合政务、企业或教学场景，避免网络用语。",
    "cheerful": "语气活泼，适度俏皮但保持礼貌，突出节日氛围。",
}


def build_prompt(holiday: HolidayOccurrence, style: str, group_context: str | None = None) -> str:
    guidance = STYLE_GUIDANCE.get(style, STYLE_GUIDANCE["warm"])
    extra_context = f"群聊背景：{group_context}\n" if group_context else ""
    payload = holiday.to_payload()
    return (
        "你是一名擅长撰写中文祝福语的助理。\n"
        f"节日名称：{payload['name']}\n"
        f"节日日期：{payload['date']}\n"
        f"节日别名：{', '.join(payload['aliases']) if payload['aliases'] else '无'}\n"
        f"风格要求：{guidance}\n"
        "请输出 1 条 40-80 字的群聊祝福，使用纯文本，适度引用节日传统或习俗，"
        "避免表情符号与过度营销语。可包含 1 句对未来的期许或祝愿。\n"
        f"{extra_context}"
    ).strip()


def build_system_prompt(style: str) -> str:
    guidance = STYLE_GUIDANCE.get(style, STYLE_GUIDANCE["warm"])
    return (
        "你正在为机器人生成节日祝福。请保持中文输出，避免使用 HTML、Markdown、表情符号，"
        "保持一句或两句平衡的祝福结构。"
        f" 风格提示：{guidance}"
    )


def extract_text_from_response(response: Any) -> str:
    if response is None:
        return ""
    for extractor in _EXTRACTION_PIPELINE:
        result = extractor(response)
        if result:
            return _sanitize_generated_text(result)
    return _sanitize_generated_text(str(response))


def _extract_from_choices(choices: Any) -> str:
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                content = choice.get("message", {}).get("content") if isinstance(choice.get("message"), dict) else None
                if isinstance(content, str) and content.strip():
                    return content.strip()
                text = choice.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return ""


def _extract_from_message_chain(chain: Any) -> str:
    if chain is None:
        return ""
    if isinstance(chain, str):
        return chain.strip()
    components = getattr(chain, "chain", None)
    texts = []
    if isinstance(components, Iterable):
        for item in components:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    if texts:
        return "".join(texts)
    if hasattr(chain, "plain_text"):
        value = getattr(chain, "plain_text")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if hasattr(chain, "message") and isinstance(chain.message, str):
        value = chain.message.strip()
        if value:
            return value
    return ""


def _extract_from_completion_payload(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if hasattr(payload, "choices"):
        text = _extract_from_choices(getattr(payload, "choices"))
        if text:
            return text
    if isinstance(payload, dict):
        text = ""
        if "choices" in payload:
            text = _extract_from_choices(payload.get("choices"))
        if text:
            return text
        for key in ("text", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _extract_from_plain_string(response: Any) -> str:
    return response.strip() if isinstance(response, str) else ""


def _extract_from_result_chain_attr(response: Any) -> str:
    chain = getattr(response, "result_chain", None)
    return _extract_from_message_chain(chain)


def _extract_from_simple_attrs(response: Any) -> str:
    for attr in ("text", "content", "message", "_completion_text"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_from_choices_attr(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices:
        return _extract_from_choices(choices)
    return ""


def _extract_from_raw_completion(response: Any) -> str:
    payload = getattr(response, "raw_completion", None)
    if payload:
        return _extract_from_completion_payload(payload)
    return ""


def _extract_from_mapping(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    for key in ("text", "content", "message", "answer"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            joined = "\n".join(map(str, value)).strip()
            if joined:
                return joined
    if "choices" in response:
        text = _extract_from_choices(response.get("choices"))
        if text:
            return text
    if "result_chain" in response:
        text = _extract_from_message_chain(response.get("result_chain"))
        if text:
            return text
    return ""


_EXTRACTION_PIPELINE: tuple[Callable[[Any], str], ...] = (
    _extract_from_plain_string,
    _extract_from_result_chain_attr,
    _extract_from_simple_attrs,
    _extract_from_raw_completion,
    _extract_from_choices_attr,
    _extract_from_mapping,
)


THOUGHT_PREFIX_PATTERN = re.compile(r"^\s*(?:思考|分析|推理|思路|思绪|chain of thought|analysis|reasoning)[:：]", re.IGNORECASE)
ANSWER_SPLIT_PATTERN = re.compile(r"(^|\n)\s*(?:答复|回答|回复|最终回答|最终答复|结论|final answer|answer)[:：]\s*", re.IGNORECASE)
ANSWER_PREFIX_PATTERN = re.compile(r"^\s*(?:答复|回答|回复|最终回答|最终答复|结论|final answer|answer)[:：]\s*", re.IGNORECASE)


def _sanitize_generated_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()

    matches = list(ANSWER_SPLIT_PATTERN.finditer(cleaned))
    if matches:
        cleaned = cleaned[matches[-1].end():].strip()

    lines = []
    for line in cleaned.splitlines():
        if THOUGHT_PREFIX_PATTERN.match(line):
            continue
        normalized = ANSWER_PREFIX_PATTERN.sub("", line)
        lines.append(normalized)

    result = "\n".join(lines).strip()
    return result or cleaned
