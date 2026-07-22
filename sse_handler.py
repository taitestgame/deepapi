import time
"""
DeepSeek API Server - Sse Handler
"""

import json

def make_chunk(completion_id: str, model: str, delta: dict,
               finish_reason=None) -> str:
    obj = {
        "id":      completion_id,
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index":         0,
            "delta":         delta,
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

def make_tool_call_chunk(completion_id: str, model: str,
                         index: int, call_id: str,
                         name: str = None, arguments: str = None,
                         account_prefix: str = "") -> str:
    """Create an SSE chunk for a tool call delta."""
    tc_delta = {"index": index}
    if call_id:
        tc_delta["id"] = call_id
        tc_delta["type"] = "function"
    if name is not None:
        tc_delta["function"] = {"name": name, "arguments": ""}
    if arguments is not None:
        tc_delta["function"] = {"arguments": arguments}

    delta = {"tool_calls": [tc_delta]}
    if call_id:
        delta["role"] = "assistant"
        delta["content"] = account_prefix if account_prefix else None

    obj = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": None,
        }],
    }
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

def _yield_text_stream(completion_id: str, model: str, text: str):
    """Yield text as SSE stream chunks (simulated streaming)."""
    yield make_chunk(completion_id, model, {"role": "assistant", "content": ""})
    chunk_size = 8
    for i in range(0, len(text), chunk_size):
        yield make_chunk(completion_id, model, {"content": text[i:i+chunk_size]})
    yield make_chunk(completion_id, model, {}, finish_reason="stop")
    yield "data: [DONE]\n\n"

