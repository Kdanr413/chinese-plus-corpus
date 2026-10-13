from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

class ModelAPITimeoutError(RuntimeError):
    """The upstream model did not reply within the configured time."""


DEFAULT_SYSTEM_PROMPT = """你是中文语料话题体系审核员。你的任务是判断文本所属话题，并维护统一、稳定的层级名称。
必须遵守：
1. 有有效参考候选时，可选择“通过”“归入现有”“新增”“不通过”。候选准确时优先“通过”。
2. 无有效参考候选时，禁止“通过”；优先从动态体系选择“归入现有”，确实无法覆盖才“新增”，文本无稳定话题时“不通过”。
3. 新增话题应优先挂到已有一级、二级话题下；只有确实无法归入时才新增上级。
4. 话题体系固定为三级。一级、二级是目录节点，不能作为最终标签；final_topic_path 必须恰好使用“一级话题 > 二级话题 > 三级叶子话题”的完整路径和动态体系中的规范名称，不得制造同义变体。
5. 判断现有话题时必须同时参考话题路径、话题说明和典型词，不能只按名称或单个关键词判断。
6. 选择“新增”时，必须优先复用已有一级、二级目录并补充三级叶子；路径必须恰好三级，同时给出能区分相邻话题的 new_topic_definition 和3—8个 new_topic_keywords；其他结论这两个字段留空。
7. 只根据提供的文本、代表词、参考候选和动态体系判断，不补充文本中没有的事实。"""

DEFAULT_USER_PROMPT = """请审核下面一条话题标注任务。

【当前动态话题体系】
{{taxonomy_json}}

【标注任务】
{{task_json}}

请返回符合指定 JSON Schema 的对象。decision 的含义：
- 通过：接受本任务的 reference_topic_path；
- 归入现有：改归当前动态体系中已经存在的路径；
- 新增：提出动态体系中尚不存在的规范完整路径；
- 不通过：文本没有稳定话题或证据不足，此时 final_topic_path 必须为空。
选择“新增”时，new_topic_definition 应说明该话题讨论的对象及边界，new_topic_keywords 提供3—8个来自本批证据的典型词；其他结论返回空说明和空数组。
必须先判断一级、再判断二级、最后判断三级，并分别填写 level_1_topic、level_2_topic、level_3_topic；final_topic_path 必须等于三者用“ > ”连接后的完整路径。三级话题优先从已有体系选择。新增说明和典型词只描述三级话题。
confidence 使用 0 到 1。审核说明应简洁、可供人工复核。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "review_decision": {"type": "string", "enum": ["通过", "不通过", "归入现有", "新增"]},
        "final_topic_path": {"type": "string"},
        "level_1_topic": {"type": "string"},
        "level_2_topic": {"type": "string"},
        "level_3_topic": {"type": "string"},
        "inappropriate_labels": {"type": "string"},
        "canonicalization_note": {"type": "string"},
        "revision_comment": {"type": "string"},
        "new_topic_definition": {"type": "string"},
        "new_topic_keywords": {
            "type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 8,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "review_decision", "final_topic_path", "level_1_topic", "level_2_topic", "level_3_topic",
        "inappropriate_labels",
        "canonicalization_note", "revision_comment", "new_topic_definition",
        "new_topic_keywords", "confidence",
    ],
    "additionalProperties": False,
}

DEFAULT_CONFIG = {
    "schema_version": "1.0",
    "api_style": "responses",
    "base_url": "https://api.openai.com/v1",
    "model": "",
    "timeout_seconds": 120,
    "max_output_tokens": 4096,
    "structured_output": "auto",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "user_prompt_template": DEFAULT_USER_PROMPT,
}


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding process environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            os.environ.setdefault(key, value)


def read_config(path: Path) -> dict:
    loaded = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    result = dict(DEFAULT_CONFIG)
    result.update({key: value for key, value in loaded.items() if key in result})
    result["base_url"] = os.getenv("LLM_BASE_URL", str(result["base_url"]))
    result["model"] = os.getenv("LLM_MODEL", str(result["model"]))
    validate_config(result)
    return result


def write_config(path: Path, config: dict) -> None:
    validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_config(config: dict) -> None:
    if config.get("api_style") not in {"responses", "chat_completions"}:
        raise ValueError("api_style 只支持 responses 或 chat_completions")
    if config.get("structured_output", "auto") not in {"auto", "strict", "prompt_only"}:
        raise ValueError("structured_output 只支持 auto、strict 或 prompt_only")
    parsed = urlparse(str(config.get("base_url", "")))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url 必须是有效的 http(s) 地址")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("非本机 API 地址必须使用 HTTPS")
    if not str(config.get("system_prompt", "")).strip():
        raise ValueError("system_prompt 不能为空")
    template = str(config.get("user_prompt_template", ""))
    if "{{task_json}}" not in template or "{{taxonomy_json}}" not in template:
        raise ValueError("用户提示词必须包含 {{task_json}} 和 {{taxonomy_json}}")
    timeout = int(config.get("timeout_seconds", 0))
    if not 5 <= timeout <= 600:
        raise ValueError("timeout_seconds 必须在 5 到 600 之间")
    max_tokens = int(config.get("max_output_tokens", 0))
    if not 100 <= max_tokens <= 10000:
        raise ValueError("max_output_tokens 必须在 100 到 10000 之间")


def safe_settings(config: dict, key_configured: bool) -> dict:
    endpoint, effective_style = resolve_api_endpoint(str(config["base_url"]), str(config["api_style"]))
    return {
        "api_style": config["api_style"],
        "base_url": config["base_url"],
        "model": config["model"],
        "timeout_seconds": config["timeout_seconds"],
        "max_output_tokens": config["max_output_tokens"],
        "structured_output": config.get("structured_output", "auto"),
        "system_prompt": config["system_prompt"],
        "user_prompt_template": config["user_prompt_template"],
        "key_configured": key_configured,
        "effective_api_style": effective_style,
        "endpoint_url": safe_endpoint_url(endpoint),
    }


def render_prompts(
    task: dict, taxonomy: dict, config: dict, output_schema: dict | None = None,
) -> tuple[str, str]:
    system_prompt = str(config["system_prompt"]).strip()
    task_json = json.dumps(task, ensure_ascii=False, indent=2)
    taxonomy_json = json.dumps(taxonomy, ensure_ascii=False, indent=2)
    user_prompt = str(config["user_prompt_template"])
    user_prompt = user_prompt.replace("{{task_json}}", task_json)
    user_prompt = user_prompt.replace("{{taxonomy_json}}", taxonomy_json)
    user_prompt = user_prompt.replace(
        "{{output_schema_json}}",
        json.dumps(output_schema or OUTPUT_SCHEMA, ensure_ascii=False, indent=2),
    )
    return system_prompt, user_prompt


def build_request(
    config: dict, system_prompt: str, user_prompt: str, output_schema: dict | None = None,
    *, structured: bool = True,
) -> tuple[str, dict]:
    endpoint, api_style = resolve_api_endpoint(str(config["base_url"]), str(config["api_style"]))
    model = str(config.get("model", "")).strip()
    if not model:
        raise ValueError("请先配置模型名称")
    schema = output_schema or OUTPUT_SCHEMA
    if api_style == "responses":
        body = {
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "max_output_tokens": int(config["max_output_tokens"]),
            "store": False,
        }
        if structured:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "topic_annotation",
                    "strict": True,
                    "schema": schema,
                }
            }
        return endpoint, body
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": int(config["max_output_tokens"]),
    }
    if structured:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "topic_annotation",
                "strict": True,
                "schema": schema,
            },
        }
    return endpoint, body


def resolve_api_endpoint(base_url: str, configured_style: str) -> tuple[str, str]:
    """Accept either an API root or a complete endpoint; an explicit endpoint wins."""
    parsed = urlsplit(str(base_url).strip())
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    # Repair the exact double-suffix produced by older versions of this application.
    if lowered.endswith("/chat/completions/responses"):
        path = path[:-len("/responses")]
        lowered = path.lower()
    if lowered.endswith("/chat/completions"):
        style = "chat_completions"
        endpoint_path = path
    elif lowered.endswith("/responses"):
        style = "responses"
        endpoint_path = path
    else:
        style = configured_style
        suffix = "/responses" if style == "responses" else "/chat/completions"
        endpoint_path = path + suffix
    return urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, parsed.query, parsed.fragment)), style


def effective_api_style(config: dict) -> str:
    return resolve_api_endpoint(str(config["base_url"]), str(config["api_style"]))[1]


def safe_endpoint_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _extract_text(response: dict, api_style: str) -> str:
    def text_value(value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "".join(filter(None, (text_value(item) for item in value))).strip()
        if isinstance(value, dict):
            for key in ("text", "value", "content", "output_text"):
                candidate = text_value(value.get(key))
                if candidate:
                    return candidate
            if any(key in value for key in ("decision", "review_decision", "topic_assignments", "final_topic_paths")):
                return json.dumps(value, ensure_ascii=False)
        return ""

    # Some compatible /responses endpoints return a Chat-Completions-shaped payload.
    choices = response.get("choices") or []
    if isinstance(choices, dict):
        choices = [choices]
    reasoning_candidates: list[str] = []
    for raw_choice in choices:
        choice = raw_choice if isinstance(raw_choice, dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = text_value(message.get("content"))
        if content:
            return content
        parsed_content = text_value(message.get("parsed"))
        if parsed_content:
            return parsed_content
        function_call = message.get("function_call") if isinstance(message.get("function_call"), dict) else {}
        arguments = text_value(function_call.get("arguments"))
        if arguments:
            return arguments
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") if isinstance(tool_call, dict) else {}
            arguments = text_value(function.get("arguments"))
            if arguments:
                return arguments
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        content = text_value(delta.get("content"))
        if content:
            return content
        content = text_value(choice.get("content"))
        if content:
            return content
        content = text_value(choice.get("text"))
        if content:
            return content
        for container in (message, delta, choice):
            for key in ("reasoning_content", "reasoning", "analysis"):
                reasoning = text_value(container.get(key))
                if reasoning:
                    reasoning_candidates.append(reasoning)
    for reasoning in reasoning_candidates:
        if "{" in reasoning and "}" in reasoning:
            return reasoning
    for key in ("output_text", "text", "content", "response"):
        direct = text_value(response.get(key))
        if direct:
            return direct
    pieces: list[str] = []
    output_items = response.get("output") or []
    if isinstance(output_items, (str, dict)):
        output_items = [output_items]
    for item in output_items:
        if isinstance(item, str):
            pieces.append(item)
            continue
        if not isinstance(item, dict):
            continue
        contents = item.get("content") or []
        if isinstance(contents, (str, dict)):
            contents = [contents]
        for content in contents:
            if isinstance(content, str):
                pieces.append(content)
            elif isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                value = text_value(content.get("text")) or text_value(content.get("content"))
                if value:
                    pieces.append(value)
        if not item.get("content"):
            value = text_value(item.get("text"))
            if value:
                pieces.append(value)
    if pieces:
        return "".join(pieces)
    for container_key in ("data", "result"):
        container = response.get(container_key)
        if isinstance(container, dict):
            try:
                return _extract_text(container, api_style)
            except ValueError:
                pass
    keys = "、".join(sorted(map(str, response.keys()))) or "无"
    status = response.get("status", "")
    incomplete = response.get("incomplete_details") or response.get("error") or ""
    choice_details = []
    for raw_choice in choices[:3]:
        choice = raw_choice if isinstance(raw_choice, dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        choice_details.append({
            "finish_reason": choice.get("finish_reason", ""),
            "choice_keys": sorted(choice.keys()),
            "message_keys": sorted(message.keys()),
            "content_type": type(content).__name__,
            "content_length": len(content) if isinstance(content, (str, list, dict)) else 0,
            "reasoning_length": len(reasoning) if isinstance(reasoning, str) else 0,
        })
    exhausted = any(
        row.get("finish_reason") in {"length", "max_tokens", "max_output_tokens"}
        for row in choice_details
    )
    advice = (
        "；模型因输出上限停止，请把‘最大输出 tokens’提高到4096—8000后重试"
        if exhausted else
        "；兼容接口返回了空的最终内容，请检查该模型是否要求关闭思考模式或提高最大输出 tokens"
    )
    raise ValueError(
        "API 响应中没有可解析的文本输出"
        f"（字段：{keys}；状态：{status}；详情：{incomplete}；choices诊断："
        f"{json.dumps(choice_details, ensure_ascii=False)}{advice}）"
    )


def _prompt_only_user_prompt(user_prompt: str, schema: dict) -> str:
    return (
        user_prompt.rstrip()
        + "\n\n接口不支持结构化输出参数。请只输出一个可被 JSON.parse 解析的 JSON 对象，"
        "不要输出 Markdown 代码围栏、解释或前后缀文字。对象必须符合以下 JSON Schema：\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def _structured_output_unsupported(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in (
        "response_format", "json_schema", "text.format", "structured output",
        "unavailable", "not supported", "unsupported",
    ))


def _request_json(config: dict, api_key: str, url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(config["timeout_seconds"])) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:2000]
        if api_key:detail=detail.replace(api_key,'[REDACTED]')
        raised = RuntimeError(f"模型 API 返回 HTTP {error.code}：{detail}")
        raised.api_detail = detail  # type: ignore[attr-defined]
        raised.api_status = error.code  # type: ignore[attr-defined]
        raise raised from error
    except TimeoutError as error:
        raise ModelAPITimeoutError(f"模型接口读取超时（{int(config['timeout_seconds'])} 秒），尚未取得完整结果。请检查接口响应速度，或调整超时后重新测试。") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason,TimeoutError):
            raise ModelAPITimeoutError(f"模型接口连接超时（{int(config['timeout_seconds'])} 秒），请检查网络和服务地址后重试。") from error
        raise RuntimeError(f"无法连接模型 API：{error.reason}") from error
    except (ConnectionError,OSError) as error:
        raise RuntimeError('模型接口连接中断，请检查网络和接口服务后重试') from error


def parse_model_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as original_error:
        decoder = json.JSONDecoder()
        value = None
        for match in re.finditer(r"\{", cleaned):
            try:
                candidate, _ = decoder.raw_decode(cleaned[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise original_error
    if not isinstance(value, dict):
        raise ValueError("模型结果必须是 JSON 对象")
    return value


def _call_model_once(
    config: dict, api_key: str, system_prompt: str, user_prompt: str,
    output_schema: dict | None = None,
) -> dict:
    if not api_key:
        raise ValueError("未配置 API Key；请在页面临时输入，或设置 OPENAI_API_KEY/LLM_API_KEY")
    schema = output_schema or OUTPUT_SCHEMA
    mode = str(config.get("structured_output", "auto"))
    structured = mode != "prompt_only"
    effective_prompt = user_prompt if structured else _prompt_only_user_prompt(user_prompt, schema)
    url, request_body = build_request(
        config, system_prompt, effective_prompt, schema, structured=structured,
    )
    fallback_used = False
    try:
        raw = _request_json(config, api_key, url, request_body)
    except RuntimeError as error:
        detail = str(getattr(error, "api_detail", error))
        if mode != "auto" or not _structured_output_unsupported(detail):
            raise
        fallback_used = True
        effective_prompt = _prompt_only_user_prompt(user_prompt, schema)
        url, request_body = build_request(
            config, system_prompt, effective_prompt, schema, structured=False,
        )
        raw = _request_json(config, api_key, url, request_body)
    try:
        text = _extract_text(raw, effective_api_style(config))
    except ValueError:
        if mode != "auto" or fallback_used:
            raise
        fallback_used = True
        effective_prompt = _prompt_only_user_prompt(user_prompt, schema)
        url, request_body = build_request(
            config, system_prompt, effective_prompt, schema, structured=False,
        )
        raw = _request_json(config, api_key, url, request_body)
        text = _extract_text(raw, effective_api_style(config))
    return {
        "parsed": parse_model_json(text),
        "output_text": text,
        "response_id": raw.get("id", ""),
        "usage": raw.get("usage", {}),
        "structured_output_mode": mode,
        "fallback_used": fallback_used,
        "effective_api_style": effective_api_style(config),
        "endpoint_url": safe_endpoint_url(url),
    }


def call_model(config: dict, api_key: str, system_prompt: str, user_prompt: str, output_schema: dict | None = None) -> dict:
    # Tests are single-item requests; batch failures are retried, recorded and
    # handled by the existing per-cluster checkpoint loop.
    attempts=max(1,min(int(config.get('_max_attempts',2)),3))
    error=None
    for attempt in range(1,attempts+1):
        try:return _call_model_once(config,api_key,system_prompt,user_prompt,output_schema)
        except Exception as exc:
            error=exc
            if getattr(exc,'api_status',None) in {400,401,403,404}:break
    if os.environ.get('CORPUS_PROJECT_ROOT'):
        from scripts.workflow.model_errors import ModelErrors
        url,body=build_request(config,system_prompt,user_prompt,output_schema or OUTPUT_SCHEMA,structured=config.get('structured_output','auto')!='prompt_only')
        ModelErrors().record('topic_review',url,body,error,api_key,{'request_attempts':attempt})
    raise error
