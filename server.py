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
    if not email or not password:
        raise ValueError("LỖI: Chưa cấu hình DEEPSEEK_EMAIL hoặc DEEPSEEK_PASSWORD trong file .env!")
    ACCOUNTS.append({
        "email":    email,
        "password": password,
        "token":    None,
    })

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
# TOKEN MANAGER
# ============================================================

_account_lock = threading.Lock()
_current_account_index = 0

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
                    print(f"[auth] Login OK cho tài khoản #{_current_account_index + 1}: {token[:20]}...")
                except Exception as e:
                    print(f"[auth] Tài khoản #{_current_account_index + 1} ({acc.get('email')}) đăng nhập lỗi: {e}")
                    # Chuyển sang tài khoản tiếp theo nếu tài khoản này lỗi đăng nhập
                    _current_account_index = (_current_account_index + 1) % len(ACCOUNTS)
                    continue
            
            token = acc["token"]
            # Xoay vòng tài khoản cho lần gọi tiếp theo
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

# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)

# CORS support for web clients
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
# PROMPT BUILDER
# ============================================================

def build_prompt(messages: list) -> str:
    parts = []
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
            parts.append(f"<system>\n{content}\n</system>")
        elif role == "user":
            parts.append(f"Human: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")

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
                     thinking_enabled: bool, completion_id: str):
    """Generator yield SSE strings theo OpenAI format"""

    sess = make_session()
    yield make_chunk(completion_id, model, {"role": "assistant", "content": ""})

    session_id     = None
    msg_id         = 0
    last_status    = ""

    try:
        session_id = create_session(token, session=sess)
        pow_resp   = get_pow(token, session=sess)

        lines = call_completion(
            token=token, session_id=session_id, prompt=prompt,
            model=model, thinking=thinking_enabled,
            pow_response=pow_resp, http_session=sess,
        )

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
                        yield make_chunk(completion_id, model, {"content": v})
                    else:
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

        yield make_chunk(completion_id, model, {}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    except Exception as e:
        invalidate_token(token)
        err = {"error": {"type": "api_error", "message": str(e)}}
        yield f"data: {json.dumps(err)}\n\n"
    finally:
        # Giữ lại lịch sử chat trên DeepSeek, không xóa session
        # if session_id:
        #     delete_session(token, session_id, http_session=sess)
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
    stream  = bool(body.get("stream", False))
    thinking_flag = body.get("thinking", None)

    if not msgs:
        return jsonify({"error": {"message": "messages required"}}), 400

    prompt = build_prompt(msgs)

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
            stream_generator(token, prompt, model, thinking_enabled, completion_id),
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
        # Giữ lại lịch sử chat trên DeepSeek, không xóa session
        # if session_id:
        #     delete_session(token, session_id, http_session=sess)
        pass

    prompt_tokens     = len(prompt) // 4
    completion_tokens = len(result.get("text", "")) // 4

    resp = {
        "id":      completion_id,
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": result.get("text", "")},
            "finish_reason": result.get("finish_reason", "stop"),
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

    # Flask threaded=True: mỗi request chạy trong thread riêng
    # Không dùng asyncio → không conflict với cloakbrowser
    app.run(
        host=host,
        port=port,
        threaded=True,
        debug=False,
    )
