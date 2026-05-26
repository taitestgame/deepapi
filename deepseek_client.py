

import json
import time
import threading
import queue
from cloakbrowser import launch
from pow_solver import solve_challenge

# ============================================================
# CONSTANTS
# ============================================================
BASE_URL = "https://chat.deepseek.com"
CLIENT_VERSION = "2.0.4"

BASE_HEADERS = {
    "Host":              "chat.deepseek.com",
    "Accept":            "application/json",
    "Content-Type":      "application/json",
    "accept-charset":    "UTF-8",
    "User-Agent":        f"DeepSeek/{CLIENT_VERSION} Android/35",
    "x-client-platform": "android",
    "x-client-version":  CLIENT_VERSION,
    "x-client-locale":   "zh_CN",
}

LOGIN_URL          = f"{BASE_URL}/api/v0/users/login"
CREATE_SESSION_URL = f"{BASE_URL}/api/v0/chat_session/create"
CREATE_POW_URL     = f"{BASE_URL}/api/v0/chat/create_pow_challenge"
COMPLETION_URL     = f"{BASE_URL}/api/v0/chat/completion"
CONTINUE_URL       = f"{BASE_URL}/api/v0/chat/continue"
DELETE_SESSION_URL = f"{BASE_URL}/api/v0/chat_session/delete"
COMPLETION_TARGET_PATH = "/api/v0/chat/completion"

MODEL_MAP = {
    "deepseek-v4-flash":  "default",
    "deepseek-v4-pro":    "expert",
    "deepseek-r2":        "expert",
    "deepseek-chat":      "default",
    "deepseek-reasoner":  "expert",
    "deepseek-v3":        "default",
    "deepseek-r1":        "expert",
}

def get_model_type(model: str) -> str:
    return MODEL_MAP.get(model.lower().strip(), "default")


# ============================================================
# BROWSER SESSION (singleton per thread)
# ============================================================

class PlaywrightWorker(threading.Thread):
    def __init__(self, fingerprint: str = "88888"):
        super().__init__(name="PlaywrightWorker", daemon=True)
        self.fingerprint = fingerprint
        self.task_queue = queue.Queue()
        self._sse_queue = queue.Queue()
        self.init_queue = queue.Queue()
        self.browser = None
        self.page = None

    def run(self):
        try:
            self.browser = launch(
                headless=True,
                humanize=False,
                args=[
                    f'--fingerprint={self.fingerprint}',
                    '--fingerprint-platform=windows',
                ]
            )
            self.page = self.browser.new_page()

            self.page.expose_function("_py_sse_chunk", self._on_sse_chunk)
            self.page.expose_function("_py_sse_done",  self._on_sse_done)

            # Log console messages from JS to Python console for debugging
            self.page.on("console", lambda msg: print(f"[browser console] {msg.type}: {msg.text}"))

            # Điều hướng vào deepseek trước để setup context/cookies + same-origin fetch
            self.page.goto(
                "https://chat.deepseek.com",
                wait_until="networkidle",
                timeout=30000
            )
            import time as _time
            _time.sleep(3)
            self.init_queue.put(("ok", None))
        except Exception as e:
            self.init_queue.put(("error", e))
            return

        while True:
            task = self.task_queue.get()
            if task is None:
                break
            action, args, resp_queue = task
            try:
                if action == "post_json":
                    url = args[0] if args else ""
                    # print(f"[worker] Executing post_json to {url}...")
                    res = self._post_json(*args)
                    # print(f"[worker] post_json to {url} done.")
                    resp_queue.put(("ok", res))
                elif action == "post_sse_stream":
                    url = args[0] if args else ""
                    # print(f"[worker] Executing post_sse_stream to {url}...")
                    self._post_sse_stream(*args, resp_queue)
                    # print(f"[worker] post_sse_stream to {url} done.")
                elif action == "close":
                    # print(f"[worker] Closing browser...")
                    if self.browser:
                        self.browser.close()
                    # print(f"[worker] Browser closed.")
                    resp_queue.put(("ok", None))
                    break
                else:
                    resp_queue.put(("error", ValueError(f"Unknown action: {action}")))
            except Exception as e:
                # print(f"[worker] Action {action} failed: {e}")
                resp_queue.put(("error", e))

    def _on_sse_chunk(self, chunk: str):
        # print(f"[worker] Callback chunk received: {chunk.strip()}")
        self._sse_queue.put(("chunk", chunk))

    def _on_sse_done(self):
        # print("[worker] Callback done received.")
        self._sse_queue.put(("done", None))

    def _post_json(self, url: str, headers: dict, payload: dict) -> dict:
        result = self.page.evaluate(
            """async ([url, headers, body]) => {
                try {
                    const resp = await fetch(url, {
                        method:  'POST',
                        headers: headers,
                        body:    body,
                    });
                    const text = await resp.text();
                    return { status: resp.status, body: text, ok: true };
                } catch(e) {
                    return { status: 0, body: '', error: e.toString(), ok: false };
                }
            }""",
            [url, headers, json.dumps(payload or {})]
        )
        if not result.get('ok'):
            raise RuntimeError(f"Fetch error: {result.get('error', 'unknown')}")
        if result['status'] >= 400:
            raise RuntimeError(f"HTTP {result['status']}: {result['body'][:300]}")
        raw = result['body']
        if not raw or not raw.strip():
            raise RuntimeError(
                f"Empty response body from {url}\n"
                f"Status: {result['status']}\n"
                f"Thử: trang chưa load xong hoặc bị bot-detect"
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"JSON parse lỗi: {e}\nRaw ({len(raw)} chars): {raw[:500]}")

    def _post_sse_stream(self, url: str, headers: dict, payload: dict, resp_queue: queue.Queue):
        self.page.evaluate(
            """async ([url, headers, body]) => {
                window._sse_chunks = [];
                window._sse_done = false;
                console.log("JS: Starting fetch to " + url);
                fetch(url, {
                    method:  'POST',
                    headers: headers,
                    body:    body,
                }).then(async resp => {
                    console.log("JS: Fetch responded with status " + resp.status);
                    if (resp.status >= 400) {
                        const errText = await resp.text();
                        window._sse_chunks.push("error: HTTP " + resp.status + ": " + errText);
                        return;
                    }
                    const reader  = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let   buffer  = '';
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\\n');
                        buffer = lines.pop();
                        for (const line of lines) {
                            window._sse_chunks.push(line + '\\n');
                        }
                    }
                    if (buffer) window._sse_chunks.push(buffer);
                }).catch(e => {
                    console.error("JS: Fetch catch error:", e.toString());
                    window._sse_chunks.push("error: " + e.toString());
                }).finally(() => {
                    console.log("JS: Fetch flow finished");
                    window._sse_done = true;
                });
            }""",
            [url, headers, json.dumps(payload or {})]
        )

        stream_done = False
        while not stream_done:
            self.page.wait_for_timeout(100)
            result = self.page.evaluate("""() => {
                const chunks = window._sse_chunks || [];
                window._sse_chunks = [];
                return { chunks: chunks, done: window._sse_done || false };
            }""")
            # print(f"[worker] Poll result: {result}")
            
            for chunk in result["chunks"]:
                if chunk.startswith("error: "):
                    resp_queue.put(("error", RuntimeError(chunk[7:])))
                    stream_done = True
                    break
                resp_queue.put(("chunk", chunk))
                
            if stream_done:
                break
                
            if result["done"]:
                stream_done = True
                resp_queue.put(("done", None))


class BrowserSession:
    """
    Một browser instance dùng cloakbrowser.
    Dùng page.evaluate(fetch...) để gọi API — fingerprint chuẩn như browser thật.
    Chạy các Playwright calls trên PlaywrightWorker thread để đảm bảo thread safety.
    """

    def __init__(self, fingerprint: str = "88888"):
        self._worker = PlaywrightWorker(fingerprint)
        self._worker.start()
        status, res = self._worker.init_queue.get()
        if status == "error":
            raise RuntimeError(f"Browser initialization failed: {res}")

    def close(self):
        try:
            resp_queue = queue.Queue()
            self._worker.task_queue.put(("close", (), resp_queue))
            resp_queue.get()
        except Exception as e:
            print(f"[browser] Close error: {e}")

    def post_json(self, url: str, extra_headers: dict = None, payload: dict = None) -> dict:
        headers = {**BASE_HEADERS, **(extra_headers or {})}
        resp_queue = queue.Queue()
        self._worker.task_queue.put(("post_json", (url, headers, payload), resp_queue))
        status, res = resp_queue.get()
        if status == "error":
            raise res
        return res

    def post_sse_stream(self, url: str, extra_headers: dict = None, payload: dict = None):
        headers = {**BASE_HEADERS, **(extra_headers or {})}
        resp_queue = queue.Queue()
        self._worker.task_queue.put(("post_sse_stream", (url, headers, payload), resp_queue))
        while True:
            status, val = resp_queue.get()
            if status == "error":
                raise val
            elif status == "done":
                break
            elif status == "chunk":
                yield val



# ============================================================
# GLOBAL SESSION POOL (đơn giản: 1 session)
# ============================================================

_default_session: BrowserSession = None
_session_lock = threading.Lock()

def get_default_session() -> BrowserSession:
    global _default_session
    with _session_lock:
        if _default_session is None:
            print("[browser] Khởi tạo browser session...")
            _default_session = BrowserSession()
            print("[browser] Sẵn sàng.")
        return _default_session

def make_session() -> BrowserSession:
    """Alias để tương thích với code cũ"""
    return get_default_session()


# ============================================================
# AUTH HEADERS
# ============================================================

def auth_headers(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


# ============================================================
# LOGIN
# ============================================================

def login(email: str = None, password: str = None,
          mobile: str = None, area_code: str = None,
          session: BrowserSession = None) -> str:
    if session is None:
        session = get_default_session()

    payload = {
        "password":  password.strip(),
        "device_id": "deepseek_to_api",
        "os":        "android",
    }
    if email:
        payload["email"] = email.strip()
    elif mobile:
        payload["mobile"] = mobile.strip()
        if area_code:
            payload["area_code"] = area_code
    else:
        raise ValueError("Cần email hoặc mobile")

    data = session.post_json(LOGIN_URL, payload=payload)

    if data.get("code") != 0:
        raise RuntimeError(f"Login thất bại: {data.get('msg')}")

    biz = data.get("data", {})
    if biz.get("biz_code", 0) != 0:
        raise RuntimeError(f"Login thất bại: {biz.get('biz_msg')}")

    token = biz.get("biz_data", {}).get("user", {}).get("token", "").strip()
    if not token:
        raise RuntimeError("Không lấy được token")
    return token


# ============================================================
# CREATE SESSION
# ============================================================

def create_session(token: str, session: BrowserSession = None) -> str:
    if session is None:
        session = get_default_session()

    data = session.post_json(
        CREATE_SESSION_URL,
        extra_headers=auth_headers(token),
        payload={"agent": "chat"}
    )
    if data.get("code") != 0:
        raise RuntimeError(f"Create session thất bại: {data.get('msg')}")

    biz = data.get("data", {}).get("biz_data", {})
    sid = biz.get("id") or biz.get("chat_session", {}).get("id", "")
    if not sid:
        raise RuntimeError("Không lấy được session_id")
    return sid.strip()


# ============================================================
# GET POW
# ============================================================

def get_pow(token: str, target_path: str = COMPLETION_TARGET_PATH,
            session: BrowserSession = None) -> str:
    if session is None:
        session = get_default_session()

    data = session.post_json(
        CREATE_POW_URL,
        extra_headers=auth_headers(token),
        payload={"target_path": target_path}
    )
    if data.get("code") != 0:
        raise RuntimeError(f"Get PoW thất bại: {data.get('msg')}")

    challenge = data.get("data", {}).get("biz_data", {}).get("challenge", {})
    return solve_challenge(challenge)


# ============================================================
# SSE PARSER (từ raw lines → dict chunks)
# ============================================================

def extract_content_recursive(items: list, default_type: str):
    parts = []
    finished = False
    for it in items:
        if not isinstance(it, dict):
            continue
        item_path = it.get("p", "")
        item_v = it.get("v")
        if item_v is None:
            continue
        if item_path in ("response/status", "status"):
            if isinstance(item_v, str) and item_v.upper() == "FINISHED":
                return [], True
            continue
        if item_path in ("response/search_status", "quasi_status", "elapsed_secs", "pending_fragment", "conversation_mode"):
            continue
        if any(pat in item_path for pat in ("quasi_status", "elapsed_secs", "pending_fragment", "conversation_mode")):
            continue
        if item_path.startswith("response/fragments/") and item_path.endswith("/status"):
            continue
            
        content = it.get("content", "")
        if content:
            frag_type = it.get("type", "").upper()
            if frag_type in ("THINK", "THINKING"):
                parts.append((content, "thinking"))
            elif frag_type == "RESPONSE":
                parts.append((content, "text"))
            else:
                parts.append((content, default_type))
            continue
            
        part_type = default_type
        if "thinking" in item_path:
            part_type = "thinking"
        elif "content" in item_path or item_path in ("response", "fragments"):
            part_type = "text"
            
        if isinstance(item_v, str):
            if not (item_path in ("response/status", "status")) and item_v != "FINISHED":
                parts.append((item_v, part_type))
        elif isinstance(item_v, list):
            for inner in item_v:
                if isinstance(inner, dict):
                    ct = inner.get("content", "")
                    if ct:
                        frag_type = inner.get("type", "").upper()
                        if frag_type in ("THINK", "THINKING"):
                            parts.append((ct, "thinking"))
                        elif frag_type == "RESPONSE":
                            parts.append((ct, "text"))
                        else:
                            parts.append((ct, part_type))
                elif isinstance(inner, str):
                    parts.append((inner, part_type))
    return parts, finished

def parse_chunk_content(chunk: dict, thinking_enabled: bool, current_type: str):
    if "v" not in chunk:
        return [], False, current_type
        
    v = chunk["v"]
    p = chunk.get("p", "")
    
    if p in ("response/search_status", "quasi_status", "elapsed_secs", "pending_fragment", "conversation_mode"):
        return [], False, current_type
    if any(pat in p for pat in ("quasi_status", "elapsed_secs", "pending_fragment", "conversation_mode")):
        return [], False, current_type
    if p.startswith("response/fragments/") and p.endswith("/status"):
        return [], False, current_type
        
    if p in ("response/status", "status") and isinstance(v, str):
        if v.upper() == "FINISHED":
            return [], True, current_type
        return [], False, current_type
        
    new_type = current_type
    
    if p == "response/content":
        new_type = "text"
    elif p == "response/thinking_content":
        if not thinking_enabled or new_type != "text":
            new_type = "thinking"
            
    parts = []
    
    if p == "response/fragments" and isinstance(v, list):
        for frag in v:
            if isinstance(frag, dict):
                frag_type = frag.get("type", "").upper()
                content = frag.get("content", "")
                if frag_type in ("THINK", "THINKING"):
                    new_type = "thinking"
                    parts.append((content, "thinking"))
                elif frag_type == "RESPONSE":
                    new_type = "text"
                    parts.append((content, "text"))
                else:
                    parts.append((content, "text"))
                    
    if p == "response" and isinstance(v, list):
        for it in v:
            if isinstance(it, dict) and it.get("p") == "fragments" and it.get("o") == "APPEND":
                frags = it.get("v", [])
                if isinstance(frags, list):
                    for frag in frags:
                        if isinstance(frag, dict):
                            frag_type = frag.get("type", "").upper()
                            if frag_type in ("THINK", "THINKING"):
                                new_type = "thinking"
                            elif frag_type == "RESPONSE":
                                new_type = "text"
                                
    part_type = "text"
    if p == "response/thinking_content":
        part_type = "thinking" if (not thinking_enabled or new_type != "text") else "text"
    elif p == "response/content":
        part_type = "text"
    elif "response/fragments" in p and "/content" in p:
        part_type = new_type
    elif p == "":
        part_type = new_type if new_type else "text"
        
    finished = False
    if isinstance(v, str):
        if v == "FINISHED" and p in ("", "status"):
            finished = True
        elif not (p in ("response/status", "status")):
            parts.append((v, part_type))
    elif isinstance(v, list):
        pp, fin = extract_content_recursive(v, part_type)
        if fin:
            finished = True
        parts.extend(pp)
    elif isinstance(v, dict):
        appended = False
        if p in ("response/content", "response/thinking_content", ""):
            text = v.get("text", "")
            if not text:
                text = v.get("content", "")
            if text:
                parts.append((text, part_type))
                appended = True
                
        if not appended:
            resp = v.get("response", v) if isinstance(v.get("response"), dict) else v
            frags = resp.get("fragments", [])
            if isinstance(frags, list):
                for item in frags:
                    if isinstance(item, dict):
                        frag_type = item.get("type", "").upper()
                        content = item.get("content", "")
                        if frag_type in ("THINK", "THINKING"):
                            new_type = "thinking"
                            parts.append((content, "thinking"))
                        elif frag_type == "RESPONSE":
                            new_type = "text"
                            parts.append((content, "text"))
                        else:
                            parts.append((content, part_type))
                            
    filtered_parts = []
    for text, p_type in parts:
        if not text:
            continue
        import re
        text = re.sub(r'(?i)</?\s*think\s*>', '', text)
        if p_type == "thinking" and not thinking_enabled:
            continue
        filtered_parts.append((text, p_type))
        
    return filtered_parts, finished, new_type

def parse_sse_lines(lines_iter):
    """Generator: yield parsed dict từ SSE data lines"""
    current_type = ""
    for line in lines_iter:
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            return
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
            
        if "response_message_id" in chunk:
            yield {"response_message_id": chunk["response_message_id"]}
            
        parts, finished, current_type = parse_chunk_content(chunk, True, current_type)
        
        for text, p_type in parts:
            if p_type == "thinking":
                yield {"p": "response/thinking_content", "v": text}
            else:
                yield {"p": "response/content", "v": text}
                
        if finished:
            yield {"p": "response/status", "v": "FINISHED"}

def parse_sse_stream(response):
    """Alias dùng với SSE line generator từ BrowserSession"""
    return parse_sse_lines(response)


# ============================================================
# CALL COMPLETION
# ============================================================

def call_completion(token: str, session_id: str, prompt: str,
                    model: str = "deepseek-v4-flash",
                    thinking: bool = False,
                    search: bool = False,
                    pow_response: str = "",
                    ref_file_ids: list = None,
                    parent_message_id=None,
                    pass_through: dict = None,
                    http_session: BrowserSession = None):
    """Trả về generator SSE lines"""
    if http_session is None:
        http_session = get_default_session()

    headers = {**auth_headers(token), "x-ds-pow-response": pow_response}
    payload = {
        "chat_session_id":   session_id,
        "model_type":        get_model_type(model),
        "parent_message_id": parent_message_id,
        "prompt":            prompt,
        "ref_file_ids":      ref_file_ids or [],
        "thinking_enabled":  thinking,
        "search_enabled":    search,
    }
    if pass_through:
        payload.update(pass_through)

    return http_session.post_sse_stream(
        COMPLETION_URL,
        extra_headers=headers,
        payload=payload
    )


# ============================================================
# CALL CONTINUE
# ============================================================

def call_continue(token: str, session_id: str, message_id: int,
                  pow_response: str = "",
                  http_session: BrowserSession = None):
    if http_session is None:
        http_session = get_default_session()

    headers = {**auth_headers(token), "x-ds-pow-response": pow_response}
    payload = {
        "chat_session_id":    session_id,
        "message_id":         message_id,
        "fallback_to_resume": True,
    }
    return http_session.post_sse_stream(
        CONTINUE_URL,
        extra_headers=headers,
        payload=payload
    )


# ============================================================
# DELETE SESSION
# ============================================================

def delete_session(token: str, session_id: str,
                   http_session: BrowserSession = None):
    if http_session is None:
        http_session = get_default_session()
    try:
        http_session.post_json(
            DELETE_SESSION_URL,
            extra_headers=auth_headers(token),
            payload={"chat_session_id": session_id}
        )
    except Exception as e:
        print(f"[browser] Delete session failed: {e}")


# ============================================================
# COLLECT FULL RESPONSE (non-stream, với auto-continue)
# ============================================================

def collect_response(token: str, session_id: str, prompt: str,
                     model: str = "deepseek-v4-flash",
                     thinking: bool = False,
                     search: bool = False,
                     http_session: BrowserSession = None,
                     max_continue_rounds: int = 8) -> dict:

    if http_session is None:
        http_session = get_default_session()

    text_parts     = []
    thinking_parts = []
    finish_reason  = "stop"
    msg_id         = 0
    last_status    = ""

    def process(lines_gen):
        nonlocal msg_id, last_status, finish_reason
        for chunk in parse_sse_lines(lines_gen):
            if chunk.get("response_message_id"):
                msg_id = int(chunk["response_message_id"])

            p = chunk.get("p", "")
            v = chunk.get("v")

            if "status" in p and isinstance(v, str):
                last_status = v
                if v.upper() == "CONTENT_FILTER":
                    finish_reason = "content_filter"

            if "auto_continue" in p and v is True:
                last_status = "AUTO_CONTINUE"

            if isinstance(v, str) and "content" in p:
                if "thinking" in p.lower():
                    thinking_parts.append(v)
                else:
                    text_parts.append(v)

    pow_resp = get_pow(token, session=http_session)
    lines = call_completion(
        token=token, session_id=session_id, prompt=prompt,
        model=model, thinking=thinking, search=search,
        pow_response=pow_resp, http_session=http_session,
    )
    process(lines)

    for rnd in range(max_continue_rounds):
        if last_status.upper() not in ("INCOMPLETE", "AUTO_CONTINUE"):
            break
        if msg_id <= 0:
            break
        print(f"[auto_continue] round {rnd+1}, msg_id={msg_id}")
        pow_resp2 = get_pow(token, session=http_session)
        cont = call_continue(token, session_id, msg_id,
                             pow_response=pow_resp2, http_session=http_session)
        last_status = ""
        process(cont)

    return {
        "text":                "".join(text_parts),
        "thinking":            "".join(thinking_parts),
        "finish_reason":       finish_reason,
        "session_id":          session_id,
        "response_message_id": msg_id,
    }
