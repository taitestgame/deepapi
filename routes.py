"""
DeepSeek API Server - Routes
"""

from flask import Flask, request, Response, jsonify, g
from config import VALID_API_KEYS, AVAILABLE_MODELS, MODEL_ALIASES, DEBUG_LOG_PATH, DEBUG_LOG_MAX_SIZE, DEBUG_LOG_KEEP, resolve_model
from token_manager import get_active_token, invalidate_token, get_account_email, get_account_password, rotate_account
from tool_parser import _extract_tool_calls_safe, strip_tool_calls, _validate_tool_calls
from prompt_builder import build_prompt, _has_xml_tools
from stream_handler import stream_generator, stream_with_tools
from deepseek_client import make_session, create_session, collect_response, get_model_type
from tool_parser import _get_valid_tool_set
from sse_handler import _yield_text_stream, make_chunk, make_tool_call_chunk
import os
import json
import time
import uuid
import threading
import re
from datetime import datetime

_request_id_counter = 0
_request_id_lock = threading.Lock()

def _generate_request_id() -> str:
    global _request_id_counter
    with _request_id_lock:
        _request_id_counter += 1
        return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_request_id_counter:04d}-{uuid.uuid4().hex[:8]}"

def _get_account_id(token: str = None) -> str:
    """Get account email from token or 'unknown'."""
    if token:
        return get_account_email(token)
    return "unknown"

def _debug_log(text: str, request_id: str = None, account_id: str = None):
    """Ghi log với auto-rotate: nếu file > MAX_SIZE thì cắt giữ KEEP bytes cuối"""
    try:
        prefix = ""
        if request_id:
            prefix += f"[{request_id}] "
        if account_id:
            prefix += f"[acc:{account_id}] "
        if prefix:
            text = prefix + text
        if os.path.exists(DEBUG_LOG_PATH) and os.path.getsize(DEBUG_LOG_PATH) > DEBUG_LOG_MAX_SIZE:
            with open(DEBUG_LOG_PATH, "rb") as f:
                f.seek(-DEBUG_LOG_KEEP, os.SEEK_END)
                tail = f.read()
            with open(DEBUG_LOG_PATH, "wb") as f:
                f.write(b"[rotated]\n")
                f.write(tail)
        with open(DEBUG_LOG_PATH, "a") as f:
            f.write(text)
    except Exception:
        pass

def _get_request_id() -> str:
    if not hasattr(g, 'request_id'):
        g.request_id = _generate_request_id()
    return g.request_id

_EMAIL_SUFFIX_RE = re.compile(r'\s*\([\w.]+\)\s*$')

def _strip_email_suffix(text: str) -> str:
    if not text:
        return text
    return _EMAIL_SUFFIX_RE.sub('', text).rstrip()

def _get_account_id_from_context() -> str:
    if not hasattr(g, 'account_id'):
        g.account_id = "unknown"
    return g.account_id

def log_req():
    if request.path == "/v1/chat/completions" and request.method == "POST":
        body = request.get_json(silent=True) or {}
        msgs = body.get("messages", [])
        req_id = _get_request_id()
        _debug_log("\n=== REQ model=%s stream=%s ===\n" % (body.get("model"), body.get("stream")), request_id=req_id)
        for m in msgs[-2:]:
            c = str(m.get("content",""))[:100]
            _debug_log("[%s] %s\n" % (m.get("role"), c), request_id=req_id)

def log_resp(response):
    if request.path == "/v1/chat/completions" and request.method == "POST":
        req_id = _get_request_id()
        acc_id = _get_account_id_from_context()
        _debug_log("RESP status=%s content_type=%s\n" % (response.status, response.content_type), request_id=req_id, account_id=acc_id)
        if response.content_type == "application/json":
            try:
                data = response.get_json()
                c = data.get("choices",[{}])[0].get("message",{}).get("content","")
                tc = data.get("choices",[{}])[0].get("message",{}).get("tool_calls")
                _debug_log("content=%s\n" % repr(c[:200] if c else c), request_id=req_id, account_id=acc_id)
                _debug_log("tool_calls=%s\n" % tc, request_id=req_id, account_id=acc_id)
            except:
                _debug_log("raw=%s\n" % response.get_data()[:200], request_id=req_id, account_id=acc_id)
        elif "text/event-stream" in (response.content_type or ""):
            data = response.get_data()
            _debug_log("sse_stream=%s\n" % data[:3000], request_id=req_id, account_id=acc_id)
        _debug_log("---\n", request_id=req_id, account_id=acc_id)
    return response

def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

def get_caller_key():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
        if key:
            return key
    key = request.headers.get("X-Api-Key", "").strip()
    if key:
        return key
    return None

def require_auth():
    key = get_caller_key()
    if not key:
        return jsonify({"error": {"message": "Missing API key", "type": "invalid_request_error"}}), 401
    if VALID_API_KEYS and key not in VALID_API_KEYS:
        return jsonify({"error": {"message": "Invalid API key", "type": "invalid_request_error"}}), 401
    return None

def health():
    return jsonify({"status": "ok"})

def list_models():
    err = require_auth()
    if err:
        return err
    data = [
        {"id": m, "object": "model", "created": 1700000000, "owned_by": "deepseek"}
        for m in AVAILABLE_MODELS
    ]
    return jsonify({"object": "list", "data": data})

def chat_completions():
    err = require_auth()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    model   = resolve_model(body.get("model", "deepseek-v4-flash"))
    msgs    = body.get("messages", [])
    stream  = bool(body.get("stream", False))
    tools   = body.get("tools", [])  # Chỉ dùng tools nếu client gửi
    thinking_flag = body.get("thinking", None)

    if not msgs:
        return jsonify({"error": {"message": "messages required"}}), 400

    # Strip any previous email suffix from messages before sending to DeepSeek
    for msg in msgs:
        for key in ('content',):
            val = msg.get(key, '')
            if isinstance(val, str):
                msg[key] = _strip_email_suffix(val)

    thinking_enabled = bool(thinking_flag) if thinking_flag is not None \
                       else (get_model_type(model) in ("reasoner", "expert"))

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    try:
        rotate_account()
        token, email = get_active_token()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": {"message": f"Auth failed: {e}"}}), 500

    g.account_id = email

    # ── STREAM MODE ──
    if stream:
        if tools:
            # Use non-stream logic with retry for rate limit handling
            prompt = build_prompt(msgs, tools)
            result = None
            last_err = None
            for _attempt in range(3):
                try:
                    sess = make_session()
                    session_id = create_session(token, session=sess)
                    result = collect_response(
                        token=token, session_id=session_id, prompt=prompt,
                        model=model, thinking=thinking_enabled, http_session=sess,
                        account_email=email, account_password=get_account_password(email),
                    )
                    break
                except Exception as e:
                    last_err = e
                    print(f"[stream-tools] Attempt {_attempt+1} failed: {e}")
                    invalidate_token(token)
                    try:
                        token, email = get_active_token(force_refresh=True)
                        g.account_id = email
                    except:
                        pass
                    time.sleep(1.5 * (_attempt + 1))  # delay tăng dần khi rate limit
            if result is None:
                err = {"error": {"type": "api_error", "message": str(last_err)}}
                def err_gen():
                    yield f"data: {json.dumps(err)}\n\n"
                return Response(err_gen(), mimetype="text/event-stream")
            text = result.get("text", "")
            tool_calls = _extract_tool_calls_safe(text)
            tool_calls, tool_error = _validate_tool_calls(tool_calls, _get_valid_tool_set(tools))
            def tool_stream_gen():
                suffix = ""
                if hasattr(g, 'account_id') and g.account_id != "unknown":
                    suffix = f"({g.account_id.split('@')[0]})"
                thinking_text = result.get("thinking", "")
                if thinking_text:
                    yield make_chunk(completion_id, model, {"role": "assistant", "reasoning_content": thinking_text + " " + suffix})
                if tool_error:
                    yield from _yield_text_stream(completion_id, model, tool_error + " " + suffix)
                elif tool_calls:
                    for i, tc in enumerate(tool_calls):
                        prefix = suffix if i == 0 else ""
                        cid = "call_" + uuid.uuid4().hex[:12]
                        yield make_tool_call_chunk(completion_id, model, i, cid, name=tc["name"], account_prefix=prefix)
                        args_str = json.dumps(tc["arguments"], ensure_ascii=False)
                        yield make_tool_call_chunk(completion_id, model, i, "", arguments=args_str, account_prefix=prefix)
                    yield make_chunk(completion_id, model, {}, finish_reason="tool_calls")
                    yield "data: [DONE]\n\n"
                else:
                    clean = strip_tool_calls(text).strip()
                    if clean:
                        text_to_stream = clean
                    else:
                        text_to_stream = text
                    yield from _yield_text_stream(completion_id, model, text_to_stream + " " + suffix)
            return Response(
                tool_stream_gen(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )
        else:
            # Collect full response first, then parse XML tool calls from text.
            # This ensures CodeAI (which sends tools as XML in system prompt, not via 'tools' param)
            # still receives proper tool_calls SSE chunks.
            prompt = build_prompt(msgs, tools if tools else None)
            result = None
            last_err = None
            for _attempt in range(3):
                try:
                    sess = make_session()
                    session_id = create_session(token, session=sess)
                    result = collect_response(
                        token=token, session_id=session_id, prompt=prompt,
                        model=model, thinking=thinking_enabled, http_session=sess,
                        account_email=email, account_password=get_account_password(email),
                    )
                    break
                except Exception as e:
                    last_err = e
                    print(f"[stream-notools] Attempt {_attempt+1} failed: {e}")
                    invalidate_token(token)
                    try:
                        token, email = get_active_token(force_refresh=True)
                        g.account_id = email
                    except:
                        pass
                    time.sleep(1.5 * (_attempt + 1))  # delay tăng dần khi rate limit
            if result is None:
                err = {"error": {"type": "api_error", "message": str(last_err)}}
                def err_gen():
                    yield f"data: {json.dumps(err)}\n\n"
                return Response(err_gen(), mimetype="text/event-stream")
            text = result.get("text", "")
            thinking = result.get("thinking", "")
            tool_calls = _extract_tool_calls_safe(text)
            # BUG: Không parse tool call từ thinking!
            # thinking chứa reasoning nội bộ của model (có thể có XML mẫu, giả lập tool call),
            # dẫn đến FALSE POSITIVE: bắt nhầm XML không phải tool call thật.
            # Model đã output tool call chính thức trong text, không cần parse thêm từ thinking.
            #
            # tool_calls_from_thinking = _extract_xml_tags(thinking)
            tool_calls_from_thinking = []  # DISABLED: tránh false positive từ thinking
            all_tool_calls = tool_calls + tool_calls_from_thinking
            all_tool_calls, tool_error = _validate_tool_calls(all_tool_calls)
            print(f"[stream-notools] text_len={len(text)}, tool_calls={tool_calls}", flush=True)
            print(f"[stream-notools] thinking_len={len(thinking)}, tool_calls_from_thinking=DISABLED", flush=True)
            print(f"[stream-notools] text[:300]={repr(text[:300])}", flush=True)
            print(f"[stream-notools] thinking[:300]={repr(thinking[:300])}", flush=True)
            def notools_stream_gen():
                suffix = ""
                if hasattr(g, 'account_id') and g.account_id != "unknown":
                    suffix = f"({g.account_id.split('@')[0]})"
                thinking_text = thinking
                if thinking_text:
                    yield make_chunk(completion_id, model, {"role": "assistant", "reasoning_content": thinking_text + " " + suffix})
                if tool_error:
                    yield from _yield_text_stream(completion_id, model, tool_error + " " + suffix)
                elif all_tool_calls:
                    for i, tc in enumerate(all_tool_calls):
                        prefix = suffix if i == 0 else ""
                        cid = "call_" + uuid.uuid4().hex[:12]
                        yield make_tool_call_chunk(completion_id, model, i, cid, name=tc["name"], account_prefix=prefix)
                        args_str = json.dumps(tc["arguments"], ensure_ascii=False)
                        yield make_tool_call_chunk(completion_id, model, i, "", arguments=args_str, account_prefix=prefix)
                    yield make_chunk(completion_id, model, {}, finish_reason="tool_calls")
                    yield "data: [DONE]\n\n"
                else:
                    clean = strip_tool_calls(text).strip()
                    if clean:
                        text_to_stream = clean
                    else:
                        text_to_stream = text
                    yield from _yield_text_stream(completion_id, model, text_to_stream + " " + suffix)
            return Response(
                notools_stream_gen(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

    # ── NON-STREAM MODE ──
    prompt = build_prompt(msgs, tools if tools else None)
    
    MAX_RETRIES = 2
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            token, email = get_active_token(force_refresh=(attempt > 0))
        except Exception as e:
            return jsonify({"error": {"message": f"Auth failed: {e}"}}), 500
        
        g.account_id = email
        
        sess = make_session()
        session_id = None
        try:
            session_id = create_session(token, session=sess)
            result = collect_response(
                token=token, session_id=session_id, prompt=prompt,
                model=model, thinking=thinking_enabled, http_session=sess,
                account_email=email, account_password=get_account_password(email),
            )
            break  # Success
        except Exception as e:
            last_error = e
            invalidate_token(token)
            if attempt < MAX_RETRIES:
                print(f"[retry] Attempt {attempt+1} failed ({e}), retrying with next account...")
                import time as _time
                _time.sleep(2)
            else:
                import traceback
                traceback.print_exc()
                return jsonify({"error": {"message": str(e)}}), 500
        finally:
            pass

    text = result.get("text", "")
    tool_calls = _extract_tool_calls_safe(text)
    # Validate: nếu tool không hợp lệ → bắn lỗi text về cho model tự sửa
    tool_calls, tool_error = _validate_tool_calls(tool_calls, _get_valid_tool_set(tools))
    if tool_error:
        final_text = tool_error
    elif tool_calls:
        final_text = None
    else:
        final_text = text

    suffix = ""
    if hasattr(g, 'account_id') and g.account_id != "unknown":
        suffix = f"({g.account_id.split('@')[0]})"
    if final_text:
        final_text = final_text + " " + suffix

    prompt_tokens     = len(prompt) // 4
    completion_tokens = len(final_text or "") // 4

    resp = {
        "id":      completion_id,
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": final_text},
            "finish_reason": "tool_calls" if (tool_calls and not tool_error) else result.get("finish_reason", "stop"),
        }],
        "usage": {
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":      prompt_tokens + completion_tokens,
        },
    }

    if tool_calls:
        openai_tc = []
        for tc in tool_calls:
            openai_tc.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False)
                }
            })
        resp["choices"][0]["message"]["tool_calls"] = openai_tc

    if result.get("thinking"):
        resp["choices"][0]["message"]["reasoning_content"] = result["thinking"] + " " + suffix

    return jsonify(resp)

