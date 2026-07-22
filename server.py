"""
DeepSeek API Server - OpenAI Compatible
Flask WSGI server (không dùng asyncio, không conflict với cloakbrowser)
"""

import sys
import os

# Force UTF-8 encoding for stdout and stderr on Windows to avoid UnicodeEncodeError
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

def load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("=", 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    os.environ[key] = val

load_env()

import json
import re
import time
import uuid
import threading
from flask import Flask, request, Response, jsonify

from deepseek_client import (
    login, create_session, get_pow,
    call_completion, call_continue,
    delete_session, parse_sse_lines,
    collect_response, make_session, get_model_type,
)

# ============================================================
# CONFIG
# ============================================================

VALID_API_KEYS = {
    os.environ.get("API_KEY", "sk-my-secret-key-1"),
}

ACCOUNTS = []
accounts_env = os.environ.get("DEEPSEEK_ACCOUNTS", "")
if accounts_env:
    for acc_str in accounts_env.split(","):
        acc_str = acc_str.strip()
        if ":" in acc_str:
            parts = acc_str.split(":", 1)
            ACCOUNTS.append({
                "email": parts[0].strip(),
                "password": parts[1].strip(),
                "token": None
            })

if not ACCOUNTS:
    email = os.environ.get("DEEPSEEK_EMAIL", "").strip()
    password = os.environ.get("DEEPSEEK_PASSWORD", "").strip()
    if email and password:
        ACCOUNTS.append({
            "email":    email,
            "password": password,
            "token":    None,
        })
    else:
        print("[warn] Chưa cấu hình DEEPSEEK_EMAIL hoặc DEEPSEEK_PASSWORD trong file .env")

AVAILABLE_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-r1",
    "deepseek-v3",
    # Qwen aliases (cho Qwen Code Companion)
    "qwen-plus",
    "qwen-max",
    "qwen-turbo",
    "qwen2.5-coder-32b-instruct",
    "qwen2.5-72b-instruct",
]

MODEL_ALIASES = {
    # OpenAI aliases
    "gpt-4o":        "deepseek-v4-flash",
    "gpt-4":         "deepseek-v4-flash",
    "gpt-3.5-turbo": "deepseek-v4-flash",
    "o3":            "deepseek-v4-pro",
    "o1":            "deepseek-reasoner",
    # Qwen Code Companion aliases → DeepSeek models
    "qwen-plus":                     "deepseek-v4-flash",
    "qwen-turbo":                    "deepseek-v4-flash",
    "qwen-max":                      "deepseek-v4-pro",
    "qwen2.5-coder-32b-instruct":    "deepseek-v4-flash",
    "qwen2.5-72b-instruct":          "deepseek-v4-pro",
    "qwen2.5-coder-7b-instruct":     "deepseek-v4-flash",
    "qwen-coder-plus":               "deepseek-v4-flash",
    "qwen-coder-turbo":              "deepseek-v4-flash",
    "qwen-long":                     "deepseek-v4-pro",
}

# ============================================================
# TOKEN MANAGER (WITH DISK CACHE)
# ============================================================

TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".tokens.json")
_account_lock = threading.Lock()
_current_account_index = 0

def load_cached_tokens():
    if os.path.exists(TOKEN_CACHE_FILE):
        try:
            with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                for acc in ACCOUNTS:
                    email = acc.get("email")
                    if email in cached and cached[email]:
                        acc["token"] = cached[email]
                        print(f"[auth] Đã nạp token đệm từ file cho: {email}")
        except Exception as e:
            print(f"[auth] Không thể đọc token đệm: {e}")

def save_cached_tokens():
    try:
        data = {acc["email"]: acc["token"] for acc in ACCOUNTS if acc.get("email") and acc.get("token")}
        with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[auth] Không thể lưu token đệm: {e}")

load_cached_tokens()

def get_active_token() -> str:
    global _current_account_index
    with _account_lock:
        if not ACCOUNTS:
            raise RuntimeError("Không có tài khoản DeepSeek nào được cấu hình!")
            
        for _ in range(len(ACCOUNTS)):
            acc = ACCOUNTS[_current_account_index]
            if not acc.get("token"):
                try:
                    print(f"[auth] Đang login tài khoản #{_current_account_index + 1}: {acc.get('email')}")
                    token = login(
                        email=acc.get("email"),
                        password=acc.get("password")
                    )
                    acc["token"] = token
                    save_cached_tokens()
                    print(f"[auth] Login OK cho tài khoản #{_current_account_index + 1}: {token[:20]}...")
                except Exception as e:
                    print(f"[auth] Tài khoản #{_current_account_index + 1} ({acc.get('email')}) đăng nhập lỗi: {e}")
                    _current_account_index = (_current_account_index + 1) % len(ACCOUNTS)
                    continue
            
            token = acc["token"]
            _current_account_index = (_current_account_index + 1) % len(ACCOUNTS)
            return token
            
        raise RuntimeError("Tất cả các tài khoản DeepSeek được cấu hình đều đăng nhập thất bại!")

def invalidate_token(token: str = None):
    with _account_lock:
        if token:
            for acc in ACCOUNTS:
                if acc.get("token") == token:
                    print(f"[auth] Invalidate token của tài khoản: {acc.get('email')}")
                    acc["token"] = None
                    break
        else:
            for acc in ACCOUNTS:
                acc["token"] = None
        save_cached_tokens()

# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ============================================================
# AUTH
# ============================================================

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

# ============================================================
# PROMPT BUILDER & TOOL CALL PARSER
# ============================================================

def parse_tool_calls_from_text(text: str):
    if not text:
        return False, [], text

    tool_calls = []
    clean_text = text

    # 1. Match all ```json_tool_call ... ``` blocks
    pattern_json_tool_call = r"```json_tool_call\s*(\{[\s\S]*?\})\s*```"
    matches = re.findall(pattern_json_tool_call, text)
    if matches:
        for m in matches:
            try:
                data = json.loads(m)
                if isinstance(data, dict) and "name" in data:
                    func_name = data["name"]
                    args = data.get("arguments", {})
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    tool_calls.append({
                        "id": f"call_{uuid.uuid4().hex[:16]}",
                        "type": "function",
                        "function": {
                            "name": func_name,
                            "arguments": args
                        }
                    })
            except Exception as e:
                print(f"[tool_parser] JSON parse error in block: {e}")
        
        if tool_calls:
            clean_text = re.sub(pattern_json_tool_call, "", text).strip()
            return True, tool_calls, clean_text

    # 2. Match general ```json ... ``` blocks containing "name" and "arguments"
    pattern_general_json = r"```json\s*(\{[\s\S]*?\"name\"[\s\S]*?\})\s*```"
    matches_general = re.findall(pattern_general_json, text)
    if matches_general:
        for m in matches_general:
            try:
                data = json.loads(m)
                if isinstance(data, dict) and "name" in data:
                    func_name = data["name"]
                    args = data.get("arguments", {})
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    tool_calls.append({
                        "id": f"call_{uuid.uuid4().hex[:16]}",
                        "type": "function",
                        "function": {
                            "name": func_name,
                            "arguments": args
                        }
                    })
            except Exception:
                pass
        
        if tool_calls:
            clean_text = re.sub(pattern_general_json, "", text).strip()
            return True, tool_calls, clean_text

    # 3. Fallback: Entire text is a JSON object or array of objects
    stripped = text.strip()
    if (stripped.startswith("{") or stripped.startswith("[")) and '"name"' in stripped:
        try:
            data = json.loads(stripped)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item:
                        func_name = item["name"]
                        args = item.get("arguments", {})
                        if not isinstance(args, str):
                            args = json.dumps(args, ensure_ascii=False)
                        tool_calls.append({
                            "id": f"call_{uuid.uuid4().hex[:16]}",
                            "type": "function",
                            "function": {
                                "name": func_name,
                                "arguments": args
                            }
                        })
                if tool_calls:
                    return True, tool_calls, ""
            elif isinstance(data, dict) and "name" in data:
                func_name = data["name"]
                args = data.get("arguments", {})
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "arguments": args
                    }
                })
                return True, tool_calls, ""
        except Exception:
            pass

    return False, [], text


def build_prompt(messages: list, tools: list = None) -> str:
    parts = []
    
    tool_system_prompt = ""
    if tools and isinstance(tools, list):
        tool_defs = []
        for t in tools:
            if isinstance(t, dict):
                if t.get("type") == "function" and "function" in t:
                    tool_defs.append(t["function"])
                else:
                    tool_defs.append(t)
        
        if tool_defs:
            tool_system_prompt = (
                "\n\n[AVAILABLE TOOLS]\n"
                "You have access to the following tools:\n"
                "```json\n"
                f"{json.dumps(tool_defs, indent=2, ensure_ascii=False)}\n"
                "```\n\n"
                "[TOOL CALLING INSTRUCTIONS]\n"
                "If you need to call a tool, respond ONLY with a JSON block in the exact format:\n"
                "```json_tool_call\n"
                "{\n"
                '  "name": "function_name",\n'
                '  "arguments": { "param1": "value1" }\n'
                "}\n"
                "```\n"
                "If no tool call is needed, respond normally with plain text."
            )

    has_system_msg = False
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            texts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = "\n".join(texts)
        elif not isinstance(content, str):
            content = str(content)

        if role == "system":
            has_system_msg = True
            combined_sys = content + tool_system_prompt if tool_system_prompt else content
            parts.append(f"<system>\n{combined_sys}\n</system>")
        elif role == "user":
            parts.append(f"Human: {content}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                tc_str_list = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", "")
                    tc_str_list.append(f'```json_tool_call\n{{\n  "name": "{fn_name}",\n  "arguments": {fn_args}\n}}\n```')
                tc_str = "\n".join(tc_str_list)
                if content:
                    content = f"{content}\n{tc_str}"
                else:
                    content = tc_str
            parts.append(f"Assistant: {content}")
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            parts.append(f"Human: [Tool Result for {tool_call_id}]: {content}")

    if not has_system_msg and tool_system_prompt:
        parts.insert(0, f"<system>\n{tool_system_prompt}\n</system>")

    parts.append("Assistant:")
    return "\n\n".join(parts)

def resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model.strip().lower(), model.strip())

# ============================================================
# SSE CHUNK FORMATTER
# ============================================================

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
    return f"data: {json.dumps(obj)}\n\n"

# ============================================================
# STREAM GENERATOR
# ============================================================

def stream_generator(token: str, prompt: str, model: str,
                      thinking_enabled: bool, completion_id: str,
                      has_tools: bool = False):
    """Generator yield SSE strings theo OpenAI format"""

    sess = make_session()
    yield make_chunk(completion_id, model, {"role": "assistant", "content": ""})

    session_id     = None
    msg_id         = 0
    last_status    = ""
    accumulated_text = ""

    try:
        session_id = create_session(token, session=sess)
        pow_resp   = get_pow(token, session=sess)

        lines = call_completion(
            token=token, session_id=session_id, prompt=prompt,
            model=model, thinking=thinking_enabled,
            pow_response=pow_resp, http_session=sess,
        )

        def consume(lines_gen):
            nonlocal msg_id, last_status, accumulated_text
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
                        yield make_chunk(completion_id, model, {"content": v})
                    else:
                        accumulated_text += v
                        if not has_tools:
                            yield make_chunk(completion_id, model, {"content": v})

        yield from consume(lines)

        # Auto-continue
        for rnd in range(8):
            if last_status.upper() not in ("INCOMPLETE", "AUTO_CONTINUE"):
                break
            if msg_id <= 0:
                break
            print(f"[auto_continue] round {rnd+1}, msg_id={msg_id}")
            pow2 = get_pow(token, session=sess)
            cont = call_continue(token, session_id, msg_id,
                                 pow_response=pow2, http_session=sess)
            last_status = ""
            yield from consume(cont)

        if has_tools:
            has_tool_call, tool_calls, clean_text = parse_tool_calls_from_text(accumulated_text)
            if has_tool_call:
                yield make_chunk(completion_id, model, {"tool_calls": tool_calls}, finish_reason="tool_calls")
            else:
                if clean_text:
                    yield make_chunk(completion_id, model, {"content": clean_text})
                yield make_chunk(completion_id, model, {}, finish_reason="stop")
        else:
            yield make_chunk(completion_id, model, {}, finish_reason="stop")

        yield "data: [DONE]\n\n"

    except Exception as e:
        invalidate_token(token)
        err = {"error": {"type": "api_error", "message": str(e)}}
        yield f"data: {json.dumps(err)}\n\n"
    finally:
        pass

# ============================================================
# ROUTES
# ============================================================

@app.get("/healthz")
@app.get("/readyz")
def health():
    return jsonify({"status": "ok"})


@app.get("/v1/models")
@app.get("/models")
def list_models():
    err = require_auth()
    if err:
        return err
    data = [
        {"id": m, "object": "model", "created": 1700000000, "owned_by": "deepseek"}
        for m in AVAILABLE_MODELS
    ]
    return jsonify({"object": "list", "data": data})


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
def chat_completions():
    err = require_auth()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    model   = resolve_model(body.get("model", "deepseek-v4-flash"))
    msgs    = body.get("messages", [])
    tools   = body.get("tools", None)
    stream  = bool(body.get("stream", False))
    thinking_flag = body.get("thinking", None)

    if not msgs:
        return jsonify({"error": {"message": "messages required"}}), 400

    prompt = build_prompt(msgs, tools=tools)

    thinking_enabled = bool(thinking_flag) if thinking_flag is not None \
                       else (get_model_type(model) == "reasoner")

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    try:
        token = get_active_token()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": {"message": f"Auth failed: {e}"}}), 500

    # ── STREAM MODE ──
    if stream:
        return Response(
            stream_generator(token, prompt, model, thinking_enabled, completion_id, has_tools=bool(tools)),
            mimetype="text/event-stream",
            headers={
                "Cache-Control":    "no-cache",
                "X-Accel-Buffering": "no",
                "Connection":       "keep-alive",
            },
        )

    # ── NON-STREAM MODE ──
    sess       = make_session()
    session_id = None
    try:
        session_id = create_session(token, session=sess)
        result = collect_response(
            token=token, session_id=session_id, prompt=prompt,
            model=model, thinking=thinking_enabled, http_session=sess,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        invalidate_token(token)
        return jsonify({"error": {"message": str(e)}}), 500
    finally:
        pass

    raw_text = result.get("text", "")
    prompt_tokens     = len(prompt) // 4
    completion_tokens = len(raw_text) // 4

    has_tool_call, tool_calls, clean_text = parse_tool_calls_from_text(raw_text) if tools else (False, [], raw_text)

    if has_tool_call:
        msg_obj = {
            "role": "assistant",
            "content": clean_text if clean_text else None,
            "tool_calls": tool_calls
        }
        finish_reason = "tool_calls"
    else:
        msg_obj = {
            "role": "assistant",
            "content": raw_text
        }
        finish_reason = result.get("finish_reason", "stop")

    resp = {
        "id":      completion_id,
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index":         0,
            "message":       msg_obj,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":      prompt_tokens + completion_tokens,
        },
    }
    if result.get("thinking"):
        resp["choices"][0]["message"]["thinking"] = result["thinking"]

    return jsonify(resp)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5001"))
    api_key = os.environ.get("API_KEY", "sk-my-secret-key-1")

    print("=" * 50)
    print("DeepSeek API Bridge (Flask) - OpenAI Compatible")
    print("=" * 50)
    print(f"Endpoint: http://{host if host != '0.0.0.0' else 'localhost'}:{port}/v1/chat/completions")
    print(f"Models:   http://{host if host != '0.0.0.0' else 'localhost'}:{port}/v1/models")
    print(f"API Key:  {api_key}")
    print("=" * 50)
    print("[Qwen Code Companion] Custom Provider settings:")
    print(f"  API Base URL : http://{host if host != '0.0.0.0' else 'localhost'}:{port}/v1")
    print(f"  API Key      : {api_key}")
    print("  Model        : qwen-plus  (hoặc deepseek-v4-flash)")
    print("=" * 50)
    print("[info] Khởi động trình duyệt và đăng nhập DeepSeek tự động trong nền...")
    threading.Thread(target=get_active_token, daemon=True).start()
    print("=" * 50)

    try:
        from waitress import serve
        print(f"[server] Khởi chạy sản phẩm WSGI server (Waitress) tại http://{host if host != '0.0.0.0' else 'localhost'}:{port}...")
        serve(app, host=host, port=port, threads=16)
    except ImportError:
        print("[server] Waitress chưa cài đặt, sử dụng Flask dev server...")
        app.run(
            host=host,
            port=port,
            threaded=True,
            debug=False,
        )
