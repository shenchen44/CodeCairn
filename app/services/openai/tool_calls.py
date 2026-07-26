from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re


_INVOKE_PATTERN = re.compile(
    r'<(?:\|{2}|｜{2})DSML(?:\|{2}|｜{2})invoke\s+name="([^"]+)">(.*?)'
    r'</(?:\|{2}|｜{2})DSML(?:\|{2}|｜{2})invoke>',
    re.DOTALL,
)
_PARAMETER_PATTERN = re.compile(
    r'<(?:\|{2}|｜{2})DSML(?:\|{2}|｜{2})parameter\s+'
    r'name="([^"]+)"([^>]*)>(.*?)'
    r'</(?:\|{2}|｜{2})DSML(?:\|{2}|｜{2})parameter>',
    re.DOTALL,
)


@dataclass(slots=True)
class ToolFunction:
    name: str
    arguments: str


@dataclass(slots=True)
class NormalizedToolCall:
    id: str
    function: ToolFunction
    synthetic: bool = False


def extract_dsml_tool_calls(content: str) -> list[NormalizedToolCall]:
    """Convert DeepSeek DSML content blocks into OpenAI-style tool calls."""
    calls: list[NormalizedToolCall] = []
    for index, invoke_match in enumerate(_INVOKE_PATTERN.finditer(content)):
        name, body = invoke_match.groups()
        arguments = {}
        for parameter_name, attributes, value in _PARAMETER_PATTERN.findall(
            body
        ):
            decoded = html.unescape(value.strip())
            if 'string="false"' in attributes:
                try:
                    arguments[parameter_name] = json.loads(decoded)
                except json.JSONDecodeError:
                    arguments[parameter_name] = decoded
            else:
                arguments[parameter_name] = decoded
        calls.append(
            NormalizedToolCall(
                id=f"dsml-call-{index + 1}",
                function=ToolFunction(
                    name=html.unescape(name),
                    arguments=json.dumps(arguments, ensure_ascii=False),
                ),
                synthetic=True,
            )
        )
    return calls


def normalized_tool_calls(message) -> list:
    native_calls = getattr(message, "tool_calls", None) or []
    if native_calls:
        return list(native_calls)
    return extract_dsml_tool_calls(getattr(message, "content", None) or "")


def append_tool_exchange(
    messages: list[dict],
    *,
    response_content: str,
    calls: list,
    dispatch,
) -> None:
    """Append native or content-encoded tool calls using provider-safe roles."""
    if calls and all(getattr(call, "synthetic", False) for call in calls):
        messages.append({"role": "assistant", "content": response_content})
        results = []
        for call in calls:
            result = dispatch(call.function.name, call.function.arguments)
            results.append(
                {
                    "tool": call.function.name,
                    "arguments": json.loads(call.function.arguments),
                    "result": result,
                }
            )
        messages.append(
            {
                "role": "user",
                "content": (
                    "Results for the requested read/tool operations follow. "
                    "Continue the task using these results:\n"
                    f"{json.dumps(results, ensure_ascii=False)}"
                ),
            }
        )
        return

    messages.append(
        {
            "role": "assistant",
            "content": response_content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in calls
            ],
        }
    )
    for call in calls:
        result = dispatch(call.function.name, call.function.arguments)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )
