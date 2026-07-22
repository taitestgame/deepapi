

import sys
import json
import time
import threading
import queue
from cloakbrowser import launch
from pow_solver import solve_challenge

# Force UTF-8 encoding for stdout and stderr on Windows to avoid UnicodeEncodeError
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

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

            # Điều hướng vào deepseek — retry tối đa 3 lần
            import time as _time
            last_err = None
            for attempt in range(1, 4):
                try:
                    print(f"[browser] Kết nối DeepSeek (lần {attempt}/3)...")
                    self.page.goto(
                        "https://chat.deepseek.com",
                        wait_until="domcontentloaded",
                        timeout=60000
                    )
                    _time.sleep(1)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    print(f"[browser] Lần {attempt} thất bại: {e}")
                    if attempt < 3:
                        _time.sleep(5)

            if last_err:
                raise last_err

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
                elif action == "solve_pow":
                    res = self._solve_pow_in_browser(*args)
                    resp_queue.put(("ok", res))
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
        import time as _time
        last_err = None
        for attempt in range(5):
            try:
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
                    # HTTP 202 Accepted often has empty body - retry after delay
                    if result['status'] == 202:
                        raise RuntimeError(
                            f"HTTP 202 Accepted (empty body) from {url} - retrying after delay"
                        )
                    raise RuntimeError(
                        f"Empty response body from {url}\n"
                        f"Status: {result['status']}\n"
                        f"Thử: trang chưa load xong hoặc bị bot-detect"
                    )
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"JSON parse lỗi: {e}\nRaw ({len(raw)} chars): {raw[:500]}")
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if "destroyed" in err_str or "navigation" in err_str or "loading" in err_str or "retrying after delay" in err_str:
                    print(f"[browser] Context error ({e}). Đợi {0.5 * (attempt + 1)}s rồi thử lại (lần {attempt+1}/5)...")
                    _time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_err

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
        poll_start = time.time()
        POLL_TIMEOUT = 300  # 5 minutes max for SSE stream

        while not stream_done:
            if time.time() - poll_start > POLL_TIMEOUT:
                resp_queue.put(("error", TimeoutError("SSE stream poll timed out (300s)")))
                break

            self.page.wait_for_timeout(100)
            result = self.page.evaluate("""() => {
                const chunks = window._sse_chunks || [];
                window._sse_chunks = [];
                return { chunks: chunks, done: window._sse_done || false };
            }""")
            
            for chunk in result["chunks"]:
                if chunk.startswith("error: "):
                    resp_queue.put(("error", RuntimeError(chunk[7:])))
                    stream_done = True
                    break
                resp_queue.put(("chunk", chunk))
                
            if stream_done:
                break
                
            if result["done"]:
                resp_queue.put(("done", None))
                stream_done = True

    def _solve_pow_in_browser(self, challenge: dict) -> int:
        challenge_hex = challenge["challenge"]
        salt = challenge["salt"]
        expire_at = int(challenge["expire_at"])
        difficulty = int(challenge.get("difficulty", 144000))

        # Run JS solver inside the browser page using Web Workers
        ans = self.page.evaluate(
            r"""async ([challengeHex, salt, expireAt, difficulty]) => {
                const workerSrc = `
                    const RC_H = new Int32Array([
                        0x00000000, 0x00000000, 0x80000000, 0x80000000,
                        0x00000000, 0x00000000, 0x80000000, 0x80000000,
                        0x00000000, 0x00000000, 0x00000000, 0x00000000,
                        0x00000000, 0x80000000, 0x80000000, 0x80000000,
                        0x80000000, 0x80000000, 0x00000000, 0x80000000,
                        0x80000000, 0x80000000, 0x00000000, 0x80000000
                    ]);
                    const RC_L = new Int32Array([
                        0x00000001, 0x00008082, 0x0000808A, 0x80008000,
                        0x0000808B, 0x80000001, 0x80008081, 0x00008009,
                        0x0000008A, 0x00000088, 0x80008009, 0x8000000A,
                        0x8000808B, 0x0000008B, 0x00008089, 0x00008003,
                        0x00008002, 0x00000080, 0x0000800A, 0x8000000A,
                        0x80008081, 0x00008080, 0x80000001, 0x80008008
                    ]);

                    function writeIntToBuf(val, buf, offset) {
                        if (val === 0) {
                            buf[offset] = 48;
                            return 1;
                        }
                        let len = 0;
                        let temp = val;
                        while (temp > 0) {
                            len++;
                            temp = (temp / 10) | 0;
                        }
                        temp = val;
                        let ptr = offset + len - 1;
                        while (temp > 0) {
                            buf[ptr] = 48 + (temp % 10);
                            ptr--;
                            temp = (temp / 10) | 0;
                        }
                        return len;
                    }

                    self.onmessage = function(e) {
                        const { challengeHex, salt, expireAt, difficulty, workerId, numWorkers } = e.data;

                        const targetBytes = new Uint8Array(challengeHex.match(/.{1,2}/g).map(byte => parseInt(byte, 16)));
                        const targetView = new DataView(targetBytes.buffer);
                        const t0_l = targetView.getInt32(0, true), t0_h = targetView.getInt32(4, true);
                        const t1_l = targetView.getInt32(8, true), t1_h = targetView.getInt32(12, true);
                        const t2_l = targetView.getInt32(16, true), t2_h = targetView.getInt32(20, true);
                        const t3_l = targetView.getInt32(24, true), t3_h = targetView.getInt32(28, true);

                        const prefix = salt + "_" + expireAt + "_";
                        const rate = 136;

                        const finalBuf = new Uint8Array(rate);
                        const prefixBytes = new TextEncoder().encode(prefix);
                        finalBuf.set(prefixBytes, 0);
                        const prefixLen = prefixBytes.length;
                        finalBuf[rate - 1] = 0x80;

                        const buf32 = new Uint32Array(finalBuf.buffer);

                        for (let n = workerId; n < difficulty; n += numWorkers) {
                            finalBuf.fill(0, prefixLen, prefixLen + 12);
                            const nonceLen = writeIntToBuf(n, finalBuf, prefixLen);
                            finalBuf[prefixLen + nonceLen] = 0x06;

                            let a0_h = buf32[1], a0_l = buf32[0];
                            let a1_h = buf32[3], a1_l = buf32[2];
                            let a2_h = buf32[5], a2_l = buf32[4];
                            let a3_h = buf32[7], a3_l = buf32[6];
                            let a4_h = buf32[9], a4_l = buf32[8];
                            let a5_h = buf32[11], a5_l = buf32[10];
                            let a6_h = buf32[13], a6_l = buf32[12];
                            let a7_h = buf32[15], a7_l = buf32[14];
                            let a8_h = buf32[17], a8_l = buf32[16];
                            let a9_h = buf32[19], a9_l = buf32[18];
                            let a10_h = buf32[21], a10_l = buf32[20];
                            let a11_h = buf32[23], a11_l = buf32[22];
                            let a12_h = buf32[25], a12_l = buf32[24];
                            let a13_h = buf32[27], a13_l = buf32[26];
                            let a14_h = buf32[29], a14_l = buf32[28];
                            let a15_h = buf32[31], a15_l = buf32[30];
                            let a16_h = buf32[33], a16_l = buf32[32];
                            let a17_h = 0, a17_l = 0;
                            let a18_h = 0, a18_l = 0;
                            let a19_h = 0, a19_l = 0;
                            let a20_h = 0, a20_l = 0;
                            let a21_h = 0, a21_l = 0;
                            let a22_h = 0, a22_l = 0;
                            let a23_h = 0, a23_l = 0;
                            let a24_h = 0, a24_l = 0;

                            for (let r = 1; r < 24; r++) {
                                // Theta
                                const c0_h = a0_h ^ a5_h ^ a10_h ^ a15_h ^ a20_h;
                                const c0_l = a0_l ^ a5_l ^ a10_l ^ a15_l ^ a20_l;
                                const c1_h = a1_h ^ a6_h ^ a11_h ^ a16_h ^ a21_h;
                                const c1_l = a1_l ^ a6_l ^ a11_l ^ a16_l ^ a21_l;
                                const c2_h = a2_h ^ a7_h ^ a12_h ^ a17_h ^ a22_h;
                                const c2_l = a2_l ^ a7_l ^ a12_l ^ a17_l ^ a22_l;
                                const c3_h = a3_h ^ a8_h ^ a13_h ^ a18_h ^ a23_h;
                                const c3_l = a3_l ^ a8_l ^ a13_l ^ a18_l ^ a23_l;
                                const c4_h = a4_h ^ a9_h ^ a14_h ^ a19_h ^ a24_h;
                                const c4_l = a4_l ^ a9_l ^ a14_l ^ a19_l ^ a24_l;

                                const d0_h = c4_h ^ ((c1_h << 1) | (c1_l >>> 31));
                                const d0_l = c4_l ^ ((c1_l << 1) | (c1_h >>> 31));
                                const d1_h = c0_h ^ ((c2_h << 1) | (c2_l >>> 31));
                                const d1_l = c0_l ^ ((c2_l << 1) | (c2_h >>> 31));
                                const d2_h = c1_h ^ ((c3_h << 1) | (c3_l >>> 31));
                                const d2_l = c1_l ^ ((c3_l << 1) | (c3_h >>> 31));
                                const d3_h = c2_h ^ ((c4_h << 1) | (c4_l >>> 31));
                                const d3_l = c2_l ^ ((c4_l << 1) | (c4_h >>> 31));
                                const d4_h = c3_h ^ ((c0_h << 1) | (c0_l >>> 31));
                                const d4_l = c3_l ^ ((c0_l << 1) | (c0_h >>> 31));

                                a0_h ^= d0_h; a0_l ^= d0_l; a5_h ^= d0_h; a5_l ^= d0_l; a10_h ^= d0_h; a10_l ^= d0_l; a15_h ^= d0_h; a15_l ^= d0_l; a20_h ^= d0_h; a20_l ^= d0_l;
                                a1_h ^= d1_h; a1_l ^= d1_l; a6_h ^= d1_h; a6_l ^= d1_l; a11_h ^= d1_h; a11_l ^= d1_l; a16_h ^= d1_h; a16_l ^= d1_l; a21_h ^= d1_h; a21_l ^= d1_l;
                                a2_h ^= d2_h; a2_l ^= d2_l; a7_h ^= d2_h; a7_l ^= d2_l; a12_h ^= d2_h; a12_l ^= d2_l; a17_h ^= d2_h; a17_l ^= d2_l; a22_h ^= d2_h; a22_l ^= d2_l;
                                a3_h ^= d3_h; a3_l ^= d3_l; a8_h ^= d3_h; a8_l ^= d3_l; a13_h ^= d3_h; a13_l ^= d3_l; a18_h ^= d3_h; a18_l ^= d3_l; a23_h ^= d3_h; a23_l ^= d3_l;
                                a4_h ^= d4_h; a4_l ^= d4_l; a9_h ^= d4_h; a9_l ^= d4_l; a14_h ^= d4_h; a14_l ^= d4_l; a19_h ^= d4_h; a19_l ^= d4_l; a24_h ^= d4_h; a24_l ^= d4_l;

                                const b0_h = a0_h, b0_l = a0_l;
                                const b10_h = (a1_h << 1) | (a1_l >>> 31), b10_l = (a1_l << 1) | (a1_h >>> 31);
                                const b20_h = (a2_l << 30) | (a2_h >>> 2), b20_l = (a2_h << 30) | (a2_l >>> 2);
                                const b5_h = (a3_h << 28) | (a3_l >>> 4), b5_l = (a3_l << 28) | (a3_h >>> 4);
                                const b15_h = (a4_h << 27) | (a4_l >>> 5), b15_l = (a4_l << 27) | (a4_h >>> 5);
                                const b16_h = (a5_l << 4) | (a5_h >>> 28), b16_l = (a5_h << 4) | (a5_l >>> 28);
                                const b1_h = (a6_l << 12) | (a6_h >>> 20), b1_l = (a6_h << 12) | (a6_l >>> 20);
                                const b11_h = (a7_h << 6) | (a7_l >>> 26), b11_l = (a7_l << 6) | (a7_h >>> 26);
                                const b21_h = (a8_l << 23) | (a8_h >>> 9), b21_l = (a8_h << 23) | (a8_l >>> 9);
                                const b6_h = (a9_h << 20) | (a9_l >>> 12), b6_l = (a9_l << 20) | (a9_h >>> 12);
                                const b7_h = (a10_h << 3) | (a10_l >>> 29), b7_l = (a10_l << 3) | (a10_h >>> 29);
                                const b17_h = (a11_h << 10) | (a11_l >>> 22), b17_l = (a11_l << 10) | (a11_h >>> 22);
                                const b2_h = (a12_l << 11) | (a12_h >>> 21), b2_l = (a12_h << 11) | (a12_l >>> 21);
                                const b12_h = (a13_h << 25) | (a13_l >>> 7), b12_l = (a13_l << 25) | (a13_h >>> 7);
                                const b22_h = (a14_l << 7) | (a14_h >>> 25), b22_l = (a14_h << 7) | (a14_l >>> 25);
                                const b23_h = (a15_l << 9) | (a15_h >>> 23), b23_l = (a15_h << 9) | (a15_l >>> 23);
                                const b8_h = (a16_l << 13) | (a16_h >>> 19), b8_l = (a16_h << 13) | (a16_l >>> 19);
                                const b18_h = (a17_h << 15) | (a17_l >>> 17), b18_l = (a17_l << 15) | (a17_h >>> 17);
                                const b3_h = (a18_h << 21) | (a18_l >>> 11), b3_l = (a18_l << 21) | (a18_h >>> 11);
                                const b13_h = (a19_h << 8) | (a19_l >>> 24), b13_l = (a19_l << 8) | (a19_h >>> 24);
                                const b14_h = (a20_h << 18) | (a20_l >>> 14), b14_l = (a20_l << 18) | (a20_h >>> 14);
                                const b24_h = (a21_h << 2) | (a21_l >>> 30), b24_l = (a21_l << 2) | (a21_h >>> 30);
                                const b9_h = (a22_l << 29) | (a22_h >>> 3), b9_l = (a22_h << 29) | (a22_l >>> 3);
                                const b19_h = (a23_l << 24) | (a23_h >>> 8), b19_l = (a23_h << 24) | (a23_l >>> 8);
                                const b4_h = (a24_h << 14) | (a24_l >>> 18), b4_l = (a24_l << 14) | (a24_h >>> 18);

                                a0_h = b0_h ^ ((~b1_h) & b2_h); a0_l = b0_l ^ ((~b1_l) & b2_l);
                                a1_h = b1_h ^ ((~b2_h) & b3_h); a1_l = b1_l ^ ((~b2_l) & b3_l);
                                a2_h = b2_h ^ ((~b3_h) & b4_h); a2_l = b2_l ^ ((~b3_l) & b4_l);
                                a3_h = b3_h ^ ((~b4_h) & b0_h); a3_l = b3_l ^ ((~b4_l) & b0_l);
                                a4_h = b4_h ^ ((~b0_h) & b1_h); a4_l = b4_l ^ ((~b0_l) & b1_l);

                                a5_h = b5_h ^ ((~b6_h) & b7_h); a5_l = b5_l ^ ((~b6_l) & b7_l);
                                a6_h = b6_h ^ ((~b7_h) & b8_h); a6_l = b6_l ^ ((~b7_l) & b8_l);
                                a7_h = b7_h ^ ((~b8_h) & b9_h); a7_l = b7_l ^ ((~b8_l) & b9_l);
                                a8_h = b8_h ^ ((~b9_h) & b5_h); a8_l = b8_l ^ ((~b9_l) & b5_l);
                                a9_h = b9_h ^ ((~b5_h) & b6_h); a9_l = b9_l ^ ((~b5_l) & b6_l);

                                a10_h = b10_h ^ ((~b11_h) & b12_h); a10_l = b10_l ^ ((~b11_l) & b12_l);
                                a11_h = b11_h ^ ((~b12_h) & b13_h); a11_l = b11_l ^ ((~b12_l) & b13_l);
                                a12_h = b12_h ^ ((~b13_h) & b14_h); a12_l = b12_l ^ ((~b13_l) & b14_l);
                                a13_h = b13_h ^ ((~b14_h) & b10_h); a13_l = b13_l ^ ((~b14_l) & b10_l);
                                a14_h = b14_h ^ ((~b10_h) & b11_h); a14_l = b14_l ^ ((~b10_l) & b11_l);

                                a15_h = b15_h ^ ((~b16_h) & b17_h); a15_l = b15_l ^ ((~b16_l) & b17_l);
                                a16_h = b16_h ^ ((~b17_h) & b18_h); a16_l = b16_l ^ ((~b17_l) & b18_l);
                                a17_h = b17_h ^ ((~b18_h) & b19_h); a17_l = b17_l ^ ((~b18_l) & b19_l);
                                a18_h = b18_h ^ ((~b19_h) & b15_h); a18_l = b18_l ^ ((~b19_l) & b15_l);
                                a19_h = b19_h ^ ((~b15_h) & b16_h); a19_l = b19_l ^ ((~b15_l) & b16_l);

                                a20_h = b20_h ^ ((~b21_h) & b22_h); a20_l = b20_l ^ ((~b21_l) & b22_l);
                                a21_h = b21_h ^ ((~b22_h) & b23_h); a21_l = b21_l ^ ((~b22_l) & b23_l);
                                a22_h = b22_h ^ ((~b23_h) & b24_h); a22_l = b22_l ^ ((~b23_l) & b24_l);
                                a23_h = b23_h ^ ((~b24_h) & b20_h); a23_l = b23_l ^ ((~b24_l) & b20_l);
                                a24_h = b24_h ^ ((~b20_h) & b21_h); a24_l = b24_l ^ ((~b20_l) & b21_l);

                                a0_h ^= RC_H[r];
                                a0_l ^= RC_L[r];
                            }

                            if (a0_l === t0_l) {
                                if (a0_h === t0_h && a1_l === t1_l && a1_h === t1_h &&
                                    a2_l === t2_l && a2_h === t2_h && a3_l === t3_l && a3_h === t3_h) {
                                    self.postMessage({ found: true, answer: n });
                                    return;
                                }
                            }
                        }
                        self.postMessage({ found: false });
                    };
                `;

                const numWorkers = Math.min(navigator.hardwareConcurrency || 4, 8);
                const workers = [];
                const blob = new Blob([workerSrc], { type: 'application/javascript' });
                const blobUrl = URL.createObjectURL(blob);

                return new Promise((resolve, reject) => {
                    let activeWorkers = numWorkers;
                    let solved = false;

                    for (let i = 0; i < numWorkers; i++) {
                        const w = new Worker(blobUrl);
                        workers.push(w);
                        w.onmessage = function(e) {
                            if (solved) return;
                            if (e.data.found) {
                                solved = true;
                                resolve(e.data.answer);
                                workers.forEach(x => x.terminate());
                                URL.revokeObjectURL(blobUrl);
                            } else {
                                activeWorkers--;
                                if (activeWorkers === 0) {
                                    reject(new Error("Nonce not found"));
                                    URL.revokeObjectURL(blobUrl);
                                }
                            }
                        };
                        w.postMessage({
                            challengeHex,
                            salt,
                            expireAt,
                            difficulty,
                            workerId: i,
                            numWorkers
                        });
                    }
                });
            }""",
            [challenge_hex, salt, expire_at, difficulty]
        )
        return ans


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

    def solve_pow_in_browser(self, challenge: dict) -> str:
        difficulty = int(challenge.get("difficulty", 144000))
        print(f"[pow] Đang giải PoW trong browser: salt={challenge.get('salt')}, difficulty={difficulty}...")
        t0 = time.time()
        
        resp_queue = queue.Queue()
        self._worker.task_queue.put(("solve_pow", (challenge,), resp_queue))
        status, answer = resp_queue.get()
        if status == "error":
            raise answer
            
        print(f"[pow] Giải PoW trong browser xong: answer={answer}, time={time.time() - t0:.2f}s")
        from pow_solver import build_pow_header
        return build_pow_header(challenge, answer)



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
    return session.solve_pow_in_browser(challenge)


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
                finished = True
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

        # ── Check for error events from DeepSeek (e.g. input_exceeds_limit) ──
        if chunk.get("type") == "error":
            err_content = chunk.get("content", "Unknown error")
            err_reason = chunk.get("finish_reason", "")
            raise RuntimeError(f"DeepSeek error: {err_content}" + (f" ({err_reason})" if err_reason else ""))
            
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
                     max_continue_rounds: int = 8,
                     account_email: str = None,
                     account_password: str = None) -> dict:

    if http_session is None:
        http_session = get_default_session()

    text_parts     = []
    thinking_parts = []
    finish_reason  = "stop"
    msg_id         = 0
    last_status    = ""
    _raw_debug     = []  # capture first few raw lines for debugging

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

    def _fsave(line):
        """Save raw line to debug buffer"""
        if len(_raw_debug) < 10:
            _raw_debug.append(str(line).strip()[:500])

    pow_resp = get_pow(token, session=http_session)
    lines = call_completion(
        token=token, session_id=session_id, prompt=prompt,
        model=model, thinking=thinking, search=search,
        pow_response=pow_resp, http_session=http_session,
    )
    # Tee: save raw lines while streaming to process()
    def _tee(gen):
        for line in gen:
            _fsave(line)
            yield line
    process(_tee(lines))

    for rnd in range(max_continue_rounds):
        if last_status.upper() not in ("INCOMPLETE", "AUTO_CONTINUE"):
            break
        if msg_id <= 0:
            break
        print(f"[auto_continue] round {rnd+1}, msg_id={msg_id}")

        # Refresh token if credentials available (may expire during long thinking)
        current_token = token
        if account_email and account_password:
            try:
                current_token = login(
                    email=account_email, password=account_password,
                    session=http_session
                )
                print(f"[auto_continue] token refreshed for {account_email}")
            except Exception as e:
                print(f"[auto_continue] token refresh failed: {e}")

        # Reuse cached PoW (saves ~3-10s per round instead of re-solving)
        cont = call_continue(current_token, session_id, msg_id,
                             pow_response=pow_resp, http_session=http_session)
        last_status = ""
        process(_tee(cont))

    final_text = "".join(text_parts)
    final_thinking = "".join(thinking_parts)
    if not final_text and not final_thinking:
        # Log raw first 3 SSE lines để debug
        import sys
        raw_info = " | ".join(_raw_debug[:5]) if _raw_debug else "(no raw lines)"
        print(f"[empty-resp] model={model} status={last_status} msg_id={msg_id} raw={raw_info}", flush=True, file=sys.stderr)
        raise RuntimeError("DeepSeek returned empty response - possible rate limit or bad token")
    return {
        "text":                final_text,
        "thinking":            final_thinking,
        "finish_reason":       finish_reason,
        "session_id":          session_id,
        "response_message_id": msg_id,
    }
