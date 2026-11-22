"""Send one structured DeepSeek request through the session budget."""
from __future__ import annotations

from contextlib import closing
import http.client
import json
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.db.business import SessionError
from app.services.budget import CloudLimits, PriceSnapshot, Usage, invoke_budgeted

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_TOKENS = 393_216
INPUT_OVERHEAD = 256
Transport = Callable[[bytes, str, int], tuple[int, bytes]]


def _post_chat(body: bytes, api_key: str, timeout_s: int) -> tuple[int, bytes]:
    with closing(http.client.HTTPSConnection("api.deepseek.com", timeout=timeout_s)) as connection:
        connection.request("POST", "/chat/completions", body=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        with closing(connection.getresponse()) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1) if response.status == 200 else b""


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid JSON number")


def _json_object(text: str | bytes) -> dict | None:
    try:
        value = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _read_usage(payload: dict) -> Usage | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    prompt, completion, total = (raw.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens"))
    if any(type(value) is not int for value in (prompt, completion, total)) or total != prompt + completion:
        return None
    details = raw.get("prompt_tokens_details")
    cache_details = details if isinstance(details, dict) else {}
    cached = raw.get("prompt_cache_hit_tokens", cache_details.get("cached_tokens", 0))
    if "cached_tokens" in cache_details:
        count = cache_details["cached_tokens"]
        if type(count) is not int or count != cached:
            return None
    try:
        usage = Usage(prompt, completion, cached)
    except SessionError:
        return None
    if "prompt_cache_miss_tokens" in raw:
        missed = raw["prompt_cache_miss_tokens"]
        if type(missed) is not int or missed != usage.input_tokens - usage.cached_input_tokens:
            return None
    return usage


def _read_content(status: int, payload: dict | None, model: str) -> dict:
    if status in {400, 401, 402, 403, 404, 413, 422}:
        raise SessionError("CONFIGURATION_ERROR", "供应商拒绝请求，请核对模型、请求参数或账户配置")
    if status in {408, 429, 500, 502, 503, 504}:
        raise SessionError("MODEL_UNAVAILABLE", "供应商暂时不可用，用量待核对")
    if status != 200:
        raise SessionError("MODEL_REQUEST_REJECTED", "供应商返回了不支持的响应状态")
    if payload is None or payload.get("model") != model:
        raise SessionError("OUTPUT_INVALID", "供应商响应格式或模型标识不匹配")
    try:
        choice, = payload["choices"]
        if choice["finish_reason"] != "stop" or choice["message"]["role"] != "assistant":
            raise ValueError("incomplete or unsupported response")
        content = choice["message"]["content"]
        value = _json_object(content) if isinstance(content, str) else None
    except (TypeError, ValueError, KeyError):
        value = None
    if value is None:
        raise SessionError("OUTPUT_INVALID", "模型未返回完整的 JSON 对象")
    return value


class DeepSeekGateway:
    def __init__(self, path: Path, settings: Settings, price: PriceSnapshot, *, transport: Transport = _post_chat,
                 max_input_tokens: int | None = None, cloud_limits: CloudLimits | None = None):
        api_key = settings.deepseek_api_key.strip()
        if not api_key or not api_key.isascii() or not api_key.isprintable() or any(char.isspace() for char in api_key):
            raise SessionError("CONFIGURATION_ERROR", "DeepSeek 凭据缺失或格式无效")
        if settings.llm_model_main != price.model or settings.llm_timeout_s < 1:
            raise SessionError("CONFIGURATION_ERROR", "模型、价格快照或超时配置无效")
        self._path = path
        self._api_key = api_key
        self._price = price
        self._timeout_s = settings.llm_timeout_s
        self._transport = transport
        self._max_input = max_input_tokens
        self._cloud_limits = cloud_limits

    def invoke_json(self, session_id: str, operation_id: str, step_key: str, *, role: str,
                    attempt_no: int, system_prompt: str, user_prompt: str, max_output_tokens: int,
                    turn_id: str | None = None) -> dict:
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise SessionError("INVALID_INPUT", "模型请求需要合法的输出 token 上限")
        if any(not isinstance(prompt, str) or not prompt.strip() for prompt in (system_prompt, user_prompt)):
            raise SessionError("INVALID_INPUT", "模型请求需要非空的系统消息和用户消息")
        messages = [{"role": "system", "content": system_prompt + "\n仅输出一个 JSON 对象。"},
                    {"role": "user", "content": user_prompt}]
        try:
            # Match the preflight estimate. The provider returns the actual token counts.
            estimated_input = sum(len(message["content"].encode("utf-8")) for message in messages) + INPUT_OVERHEAD
        except UnicodeError:
            raise SessionError("INVALID_INPUT", "模型消息必须是有效的 Unicode 文本") from None
        if self._max_input is not None and estimated_input > self._max_input:
            raise SessionError("INPUT_TOO_LARGE", "本次问答超过模型输入限额，已保留本地问答")

        def send_once(output_limit: int) -> tuple[tuple[int, dict | None, Usage | None], Usage | None]:
            body = json.dumps({
                "model": self._price.model, "messages": messages, "max_tokens": output_limit,
                "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}, "stream": False,
            }, ensure_ascii=False).encode("utf-8")
            status, content = self._transport(body, self._api_key, self._timeout_s)
            payload = _json_object(content) if status == 200 and len(content) <= MAX_RESPONSE_BYTES else None
            usage = _read_usage(payload) if payload is not None and payload.get("model") == self._price.model else None
            return (status, payload, usage), usage

        status, payload, usage = invoke_budgeted(
            self._path, session_id, operation_id, step_key, role=role, attempt_no=attempt_no, price=self._price,
            estimated_input_tokens=estimated_input, max_output_tokens=max_output_tokens, send_once=send_once,
            cloud_limits=self._cloud_limits, turn_id=turn_id,
        )
        if usage is not None and usage.output_tokens > max_output_tokens:
            raise SessionError("OUTPUT_INVALID", "供应商返回的输出用量超过请求上限")
        if usage is not None and usage.input_tokens > estimated_input:
            raise SessionError("USAGE_OVER_PLAN", "供应商输入用量超出预估，请暂停并核对")
        return _read_content(status, payload, self._price.model)
