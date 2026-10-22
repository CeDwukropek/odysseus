"""Responses wire format at the LLM boundary; agent tools keep their existing ABI.

Requests replay local history with encrypted reasoning, without server-side state.
No reasoning text or opaque replay items belong in browser events or saved prose.
"""

import copy
import json
from dataclasses import dataclass
from urllib.parse import urlparse


def endpoint_base(url: str) -> str:
    base = str(url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/models"):
        if base.endswith(suffix):
            return base[:-len(suffix)].rstrip("/")
    return base


def responses_url(url: str) -> str:
    base = endpoint_base(url)
    parsed = urlparse(base)
    if parsed.query or parsed.fragment:
        raise ValueError("Endpoint URL must not include query or fragment")
    if not parsed.path and parsed.hostname == "api.openai.com":
        base += "/v1"
    return base + "/responses"


def protocol_options(url, model, api_mode=None, reasoning_effort=None):
    """Resolve exact endpoint/model overrides, never infer protocol from a model ID."""
    from src.settings import get_setting

    configured = {}
    entries = get_setting("llm_protocol_overrides", [])
    if not isinstance(entries, list):
        raise ValueError("llm_protocol_overrides must be a list")
    for entry in entries:
        if (isinstance(entry, dict) and entry.get("model") == model
                and endpoint_base(entry.get("base_url")) == endpoint_base(url)):
            configured = entry
            break
    mode = api_mode if api_mode is not None else configured.get("api_mode") or (
        "responses" if str(url).rstrip("/").endswith("/responses") else "chat_completions"
    )
    if not isinstance(mode, str) or mode not in {"responses", "chat_completions"}:
        raise ValueError("api_mode must be responses or chat_completions")
    effort = reasoning_effort if reasoning_effort is not None else configured.get("reasoning_effort")
    if effort is not None and (not isinstance(effort, str) or effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}):
        raise ValueError("Invalid reasoning_effort; supported efforts also depend on the selected model")
    # Model capability is independent of routing. A provider rejects unsupported
    # values; we never substitute another effort or model on a validation error.
    return mode, effort


def build_input(messages):
    items = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": msg["tool_call_id"],
                          "output": msg.get("content") or ""})
            continue
        replay = msg.get("_responses_output") if role == "assistant" else None
        if replay:
            # Only replay calls that survived the existing history sanitizer.
            ids = {tc["id"] for tc in msg.get("tool_calls", [])}
            items.extend(copy.deepcopy(item) for item in replay
                         if item.get("type") != "function_call" or item.get("call_id") in ids)
            continue
        content = msg.get("content")
        if content:
            if isinstance(content, list):
                blocks = []
                for part in content:
                    kind = part.get("type")
                    if kind == "text":
                        blocks.append({"type": "input_text", "text": part["text"]})
                    elif kind == "image_url" and role == "user":
                        image = part["image_url"]
                        if isinstance(image, str):
                            image = {"url": image}
                        blocks.append({"type": "input_image", "image_url": image["url"],
                                       **({"detail": image["detail"]} if image.get("detail") else {})})
                    else:
                        raise ValueError(f"Unsupported Responses input content type: {kind}")
                content = blocks
            items.append({"role": role, "content": content})
        for tc in msg.get("tool_calls", []):
            fn = tc["function"]
            items.append({"type": "function_call", "call_id": tc["id"],
                          "name": fn["name"], "arguments": fn["arguments"]})
    return items


def build_payload(model, messages, tools=None, *, effort=None, max_tokens=0,
                  stream=False, tool_choice_none=False):
    payload = {"model": model, "input": build_input(messages), "stream": stream,
               "store": False, "include": ["reasoning.encrypted_content"]}
    if effort is not None:
        payload["reasoning"] = {"effort": effort}
    if max_tokens and max_tokens > 0:
        payload["max_output_tokens"] = max_tokens
    if tools:
        payload["tools"] = []
        for tool in tools:
            if tool.get("type") != "function":
                raise ValueError("Responses adapter supports existing function tools only")
            fn = copy.deepcopy(tool["function"])
            # Existing schemas often use optional fields. Preserve that contract
            # rather than allowing Responses to implicitly rewrite strictness.
            fn.setdefault("strict", False)
            payload["tools"].append({"type": "function", **fn})
    if tool_choice_none:
        payload["tool_choice"] = "none"
    # Do not impose sampling controls: reasoning/sampling support is model-specific.
    return payload


@dataclass
class LLMResponse:
    text: str
    tool_calls: list
    output_items: list
    usage: dict | None
    response_id: str | None
    finish_reason: str


def normalize_response(data):
    if not isinstance(data, dict):
        raise ValueError("Responses response must be an object")
    if data.get("status") not in {None, "completed", "incomplete", "failed", "cancelled"}:
        raise ValueError("Responses response has not completed")
    if data.get("error") or data.get("status") in {"failed", "cancelled"}:
        raise ValueError("Responses request failed")
    output = data.get("output", [])
    if not isinstance(output, list):
        raise ValueError("Invalid Responses output")
    text, calls, replay = [], [], []
    ids = set()
    incomplete = data.get("status") == "incomplete"
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Responses output item must be an object")
        kind = item.get("type")
        if kind == "function_call":
            if incomplete or item.get("status") not in {None, "completed"}:
                raise ValueError("Incomplete Responses function call; tools were not executed")
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not call_id or call_id in ids or not isinstance(name, str) or not name:
                raise ValueError("Invalid or duplicate Responses function call identifier/name")
            try:
                args = json.loads(item["arguments"])
                if not isinstance(args, dict):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"Invalid JSON object arguments for function {name}; tools were not executed") from None
            ids.add(call_id)
            calls.append({"id": call_id, "name": name, "arguments": item["arguments"]})
            replay.append(copy.deepcopy(item))
        elif kind == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text.append(part["text"])
                elif part.get("type") == "refusal":
                    text.append(part.get("refusal", ""))
            replay.append(copy.deepcopy(item))
        elif kind == "reasoning":
            if not item.get("encrypted_content"):
                raise ValueError("Stateless Responses continuation requires encrypted reasoning content")
            # Retain encrypted state, never summaries or plaintext reasoning.
            replay.append({k: copy.deepcopy(item[k]) for k in ("id", "type", "encrypted_content") if k in item})
            replay[-1]["summary"] = []
        else:
            raise ValueError(f"Unsupported Responses output item type: {kind}")
    return LLMResponse("".join(text), calls, replay, data.get("usage"), data.get("id"),
                       "length" if incomplete else ("tool_calls" if calls else "stop"))


class ResponsesStream:
    """Accumulate items by output index; release calls only after completion."""

    def __init__(self):
        self.items = {}
        self.text_seen = False

    def feed(self, event):
        kind = event.get("type")
        index = event.get("output_index")
        if kind == "response.output_text.delta":
            self.text_seen = True
            return event.get("delta") or ""
        if kind in {"response.output_item.added", "response.output_item.done"}:
            self.items[index] = copy.deepcopy(event["item"])
        elif kind == "response.function_call_arguments.delta":
            if index not in self.items:
                raise ValueError("Function argument delta without a Responses output item")
            self.items[index]["arguments"] = self.items[index].get("arguments", "") + event["delta"]
        elif kind == "response.function_call_arguments.done":
            if index not in self.items:
                raise ValueError("Function arguments without a Responses output item")
            self.items[index]["arguments"] = event["arguments"]
        return None

    def complete(self, response):
        data = dict(response)
        if "output" not in data:
            data["output"] = [self.items[i] for i in sorted(self.items)]
        return normalize_response(data)
