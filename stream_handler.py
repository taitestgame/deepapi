"""
DeepSeek API Server - Stream Handler
"""

from deepseek_client import call_completion, call_continue, parse_sse_lines, collect_response, make_session
from sse_handler import make_chunk, make_tool_call_chunk
from token_manager import get_active_token, invalidate_token
import time
import json

def _stream_generator_inner(token: str, prompt: str, model: str,
                           thinking_enabled: bool, completion_id: str,
                           send_first_chunk: bool = True):
    """Inner stream generator - yields SSE chunks, raises on error."""
    sess = make_session()
    if send_first_chunk:
        yield make_chunk(completion_id, model, {"role": "assistant", "content": ""})

    session_id     = None
    msg_id         = 0
    last_status    = ""

    session_id = create_session(token, session=sess)
    pow_resp   = get_pow(token, session=sess)

    # Background call + heartbeat keep-alive to prevent CLI timeout
    lines_container = []
    error_container = []
    def _bg_call():
        try:
            lines_container.append(call_completion(
                token=token, session_id=session_id, prompt=prompt,
                model=model, thinking=thinking_enabled,
                pow_response=pow_resp, http_session=sess,
            ))
        except Exception as e:
            error_container.append(e)
    _t = threading.Thread(target=_bg_call, daemon=True)
    _t.start()
    for _ in range(120):
        if lines_container or error_container:
            break
        yield ": keepalive\n\n"
        _t.join(timeout=3.0)
    _t.join(timeout=10.0)
    if error_container:
        raise error_container[0]
    if not lines_container:
        raise RuntimeError("DeepSeek call timed out")
    lines = lines_container[0]

    def consume(lines_gen):
        nonlocal msg_id, last_status
        for chunk in parse_sse_lines(lines_gen):
            if chunk.get("response_message_id"):
                msg_id = int(chunk["response_message_id"])

            p = chunk.get("p", "")
            v = chunk.get("v")

            if "status" in p and isinstance(v, str):
                last_status = v
            if "auto_continue" in p and v is True:
                last_status = "AUTO_CONTINUE"

            if isinstance(v, str) and "content" in p:
                if "thinking" in p.lower():
                    yield make_chunk(completion_id, model, {"reasoning_content": v})
                else:
                    yield make_chunk(completion_id, model, {"content": v})

    yield from consume(lines)

    # Auto-continue
    print(f"[debug] last_status={repr(last_status)}, msg_id={msg_id}")
    for rnd in range(8):
        if last_status.upper() not in ("INCOMPLETE", "AUTO_CONTINUE"):
            print(f"[debug] auto-continue break, status={repr(last_status)}")
            break
        if msg_id <= 0:
            print(f"[debug] auto-continue break, msg_id={msg_id}")
            break
        print(f"[auto_continue] round {rnd+1}, msg_id={msg_id}")
        pow2 = get_pow(token, session=sess)
        cont = call_continue(token, session_id, msg_id,
                             pow_response=pow2, http_session=sess)
        last_status = ""
        yield from consume(cont)

    if not last_status and msg_id == 0:
        raise RuntimeError(f"DeepSeek returned empty response (no content, no status, no msg_id)")

    yield make_chunk(completion_id, model, {}, finish_reason="stop")
    yield "data: [DONE]\n\n"

def stream_generator(token: str, prompt: str, model: str,
                     thinking_enabled: bool, completion_id: str):
    """Generator with auto-retry on failure."""
    MAX_RETRIES = 2
    current_token = token

    for attempt in range(MAX_RETRIES + 1):
        try:
            yield from _stream_generator_inner(
                current_token, prompt, model, thinking_enabled,
                completion_id, send_first_chunk=(attempt == 0)
            )
            return  # Success
        except Exception as e:
            invalidate_token(current_token)
            if attempt < MAX_RETRIES:
                print(f"[retry] Stream attempt {attempt+1} failed ({e}), retrying...")
                try:
                    current_token, _ = get_active_token()
                except Exception:
                    pass
                import time as _time
                _time.sleep(2)
            else:
                err = {"error": {"type": "api_error", "message": str(e)}}
                yield f"data: {json.dumps(err)}\n\n"

def stream_with_tools(token: str, msgs: list, model: str,
                      thinking_enabled: bool, completion_id: str,
                      tools: list, account_prefix: str = ""):
    """Stream response with tool call detection.
    Uses background thread to collect response + heartbeat keep-alive to prevent timeout."""
    prompt = build_prompt(msgs, tools)
    sess = make_session()

    result_container = []
    error_container = []

    def _collect():
        try:
            sid = create_session(token, session=sess)
            result_container.append(collect_response(
                token=token, session_id=sid, prompt=prompt,
                model=model, thinking=thinking_enabled, http_session=sess,
            ))
        except Exception as e:
            error_container.append(e)

    t = threading.Thread(target=_collect, daemon=True)
    t.start()

    # Keep-alive heartbeat every 3s while waiting (max 180s)
    for _ in range(60):
        if result_container or error_container:
            break
        yield ": keepalive\n\n"
        t.join(timeout=3.0)

    t.join(timeout=10.0)

    if error_container:
        invalidate_token(token)
        err = {"error": {"type": "api_error", "message": str(error_container[0])}}
        yield f"data: {json.dumps(err)}\n\n"
        return

    if not result_container:
        err = {"error": {"type": "api_error", "message": "Request timed out"}}
        yield f"data: {json.dumps(err)}\n\n"
        return

    result = result_container[0]
    text = result.get("text", "")
    tool_calls = _extract_tool_calls_safe(text)
    # Validate: nếu tool không hợp lệ → bắn lỗi text về cho model tự sửa
    tool_calls, tool_error = _validate_tool_calls(tool_calls, _get_valid_tool_set(tools))

    # Stream reasoning_content if present
    thinking_text = result.get("thinking", "")
    if thinking_text:
        yield make_chunk(completion_id, model, {"role": "assistant", "reasoning_content": thinking_text})

    if tool_error:
        yield from _yield_text_stream(completion_id, model, tool_error + " " + account_prefix)
    elif tool_calls:
        for i, tc in enumerate(tool_calls):
            cid = f"call_{uuid.uuid4().hex[:12]}"
            yield make_tool_call_chunk(completion_id, model, i, cid, name=tc["name"])
            args = json.dumps(tc["arguments"], ensure_ascii=False)
            yield make_tool_call_chunk(completion_id, model, i, "", arguments=args)

        yield make_chunk(completion_id, model, {}, finish_reason="tool_calls")
        yield "data: [DONE]\n\n"
    else:
        clean = strip_tool_calls(text).strip()
        if clean:
            yield from _yield_text_stream(completion_id, model, clean + " " + account_prefix)
        else:
            yield from _yield_text_stream(completion_id, model, text + " " + account_prefix)

