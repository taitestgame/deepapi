# DeepAPI — OpenAI-Compatible API Bridge for DeepSeek

> **⚠️ TUYÊN BỐ MIỄN TRỪ TRÁCH NHIỆM**
>
> Dự án này chỉ phục vụ **mục đích nghiên cứu, học tập và thử nghiệm cá nhân**.
> Người dùng chịu hoàn toàn trách nhiệm khi sử dụng. Không được dùng cho mục đích
> thương mại hoặc vi phạm điều khoản dịch vụ của bên thứ ba.

---

## Giới thiệu

DeepAPI là REST API bridge tương thích OpenAI, cho phép gọi DeepSeek Chat thông qua
giao diện chuẩn `/v1/chat/completions`. Hỗ trợ **tool calling** (function calling)
qua XML parsing, streaming, multi-account, và tự động xoay vòng tài khoản.

Nguồn tham khảo:
- [lessweb/deepcode-cli](https://github.com/lessweb/deepcode-cli.git)
- [taitestgame/deepapi](https://github.com/taitestgame/deepapi.git)

Ứng dụng cho:
- [deepcode-cli](https://github.com/lessweb/deepcode-cli.git) — CLI client cho DeepSeek
- [opencode](https://opencode.ai) — CLI agent mã nguồn mở

## Tính năng

- ✅ API tương thích OpenAI (`/v1/models`, `/v1/chat/completions`)
- ✅ **Tool Calling** — parse XML tool calls từ model output (28+ định dạng)
- ✅ Streaming & Non-streaming SSE
- ✅ Multi-account round-robin (xoay vòng sau mỗi request)
- ✅ Tự động refresh token (10 phút / lần)
- ✅ Auto-continue khi response dài (tối đa 8 vòng)
- ✅ PoW solver qua cloakbrowser (Chromium headless)
- ✅ Hỗ trợ model: deepseek-v4-flash, deepseek-v4-pro, deepseek-chat, deepseek-reasoner
- ✅ Tự động inject tool descriptions vào system prompt
- ✅ Strip tool call XML khỏi text response (giữ nguyên code blocks)

## Yêu cầu

- **Python** 3.12+
- **Node.js** 24+
- **Ubuntu 24.04** (hoặc Linux tương tự)
- **Playwright / Chromium** (cho PoW solver)

## Cài đặt

### 1. Cài dependencies

```bash
# Python
pip install --break-system-packages flask>=3.0.0 cloakbrowser>=0.3.30

# Node.js + Playwright
curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
sudo apt-get install -y nodejs
npx playwright install-deps chromium
npx playwright install chromium
```

### 2. Cấu hình tài khoản

Tạo file `.env` trong thư mục dự án:

```ini
# Tài khoản DeepSeek (email:password, phân cách bằng dấu phẩy)
DEEPSEEK_ACCOUNTS=email1@gmail.com:password1,email2@gmail.com:password2

# API key cho client gọi vào server này
API_KEY=deepcode2026
PORT=5001
HOST=0.0.0.0
```

### 3. Chạy server

```bash
# Chạy trực tiếp
python3 server.py

# Hoặc dùng systemd (khuyến nghị)
sudo tee /etc/systemd/system/deepapi.service << 'EOF'
[Unit]
Description=DeepSeek API Server
After=network.target

[Service]
Type=simple
User=vps2
WorkingDirectory=/home/vps2/deepapi
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
ExecStart=/usr/bin/python3 /home/vps2/deepapi/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now deepapi
```

## Sử dụng

### Liệt kê models

```bash
curl http://localhost:5001/v1/models \
  -H "Authorization: Bearer deepcode2026"
```

### Chat completion (stream)

```bash
curl http://localhost:5001/v1/chat/completions \
  -H "Authorization: Bearer deepcode2026" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-pro",
    "stream": true,
    "messages": [{"role": "user", "content": "Xin chào"}]
  }'
```

### Chat completion (non-stream)

```bash
curl http://localhost:5001/v1/chat/completions \
  -H "Authorization: Bearer deepcode2026" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-pro",
    "stream": false,
    "messages": [{"role": "user", "content": "1+1=?"}]
  }'
```

### Tool calling (OpenAI format)

```bash
curl http://localhost:5001/v1/chat/completions \
  -H "Authorization: Bearer deepcode2026" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-pro",
    "stream": true,
    "messages": [{"role": "user", "content": "tạo file /home/user/test.txt nội dung hello"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "write",
        "description": "Create a file",
        "parameters": {
          "type": "object",
          "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"}
          },
          "required": ["file_path", "content"]
        }
      }
    }]
  }'
```

### Cấu hình OpenCode

OpenCode CLI sử dụng file `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "deepapi": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DeepAPI",
      "options": {
        "baseURL": "http://127.0.0.1:5001/v1",
        "apiKey": "deepcode2026"
      },
      "models": {
        "deepseek-chat": {
          "name": "DeepAPI Chat"
        },
        "deepseek-v4-flash": {
          "name": "DeepAPI V4 Flash",
          "options": {
            "thinking": { "type": "enabled" },
            "reasoning": { "effort": "medium" }
          }
        },
        "deepseek-v4-pro": {
          "name": "DeepAPI V4 Pro",
          "options": {
            "thinking": { "type": "enabled" },
            "reasoning": { "effort": "high" }
          }
        }
      }
    }
  },
  "model": "deepapi/deepseek-v4-flash"
}
```

| Trường | Giá trị | Giải thích |
|--------|---------|------------|
| `provider.<name>.npm` | `@ai-sdk/openai-compatible` | Provider package cho OpenAI‑compatible API |
| `provider.<name>.options.baseURL` | `http://127.0.0.1:5001/v1` | URL của DeepAPI server |
| `provider.<name>.options.apiKey` | `deepcode2026` | API key (khớp với `API_KEY` trong `.env`) |
| `model` | `deepapi/<model_name>` | Model mặc định, format: `provider_id/model_id` |
| `thinking.type` | `"enabled"` | Bật reasoning content |
| `reasoning.effort` | `"high"`, `"medium"`, `"low"` | Mức reasoning effort |

#### Chọn model mặc định

Đổi `model` để chọn model khởi động:
- `"deepapi/deepseek-v4-flash"` — nhanh, phù hợp task đơn giản
- `"deepapi/deepseek-v4-pro"` — mạnh, phù hợp task phức tạp
- `"deepapi/deepseek-chat"` — không thinking (chat thuần)

#### Kiểm tra kết nối

```bash
opencode "Hello, world!"
```

Nếu có lỗi auth, kiểm tra:
- Server đã chạy chưa? `curl http://127.0.0.1:5001/v1/models -H "Authorization: Bearer deepcode2026"`
- `baseURL` có đúng không? Phải kết thúc bằng `/v1`
- `apiKey` trong config có khớp với `API_KEY` trong `.env` không?

### Cấu hình DeepCode CLI

Trong `~/.deepcode/settings.json`:

```json
{
  "env": {
    "MODEL": "deepseek-v4-pro",
    "BASE_URL": "http://localhost:5001/v1",
    "API_KEY": "deepcode2026"
  },
  "thinkingEnabled": true,
  "reasoningEffort": "max"
}
```

## Kiến trúc

```
Client → POST /v1/chat/completions (OpenAI format)
    → server.py: auth → rotate account → build_prompt → call DeepSeek
    → deepseek_client.py: cloakbrowser → fetch() DeepSeek API
    → parse_sse_lines() → tách text, thinking, tool calls
    → tool_parser.py: parse XML tool calls (28+ formats)
    → Nếu có XML tool calls → convert sang OpenAI tool_calls format
    → Response: SSE stream hoặc JSON
```

### Account rotation

```
Mỗi request:
  1. rotate_account() → tăng index (vòng tròn)
  2. get_active_token() → lấy token của account đó
  3. Xử lý request với account cố định (kể cả auto-continue + tool call)

Khi lỗi:
  - invalidate_token() → đánh dấu token hỏng
  - get_active_token(force_refresh=True) → login lại hoặc chuyển account kế

Refresh định kỳ:
  - Thread nền login lại tất cả accounts mỗi 600 giây
```

## Tool Calling

Server hỗ trợ **28+ định dạng XML** tool calls từ model output. Các định dạng chính:

| Format | Ví dụ |
|--------|-------|
| Format 1 (chuẩn) | `<tool>bash</tool><json>{"command":"ls"}</json>` |
| Format 2 (legacy) | `<function_call name="bash"><args>{"command":"ls"}</args></function_call>` |
| Format 3 (CodeAI) | `<tool><name>bash</name><parameter name="command" string="true">ls</parameter></tool>` |
| Format 4 (bare) | `<bash>ls</bash>` |
| Format 6 (nested) | `<tool><bash><json>{"command":"ls"}</json></bash></tool>` |
| Format 10 | `<tool><tool_call>bash</tool_call><json>{"command":"ls"}</json></tool>` |
| Format 12 (raw JSON) | `<tool>{"name":"bash","command":"ls"}</tool>` |
| Format 26 (multi) | `<tools><tool>bash</tool><json>{"cmd":"ls"}</json>...</tools>` |
| Format 27 (bracket) | `[bash] ls [/bash]` |
| Format 28 (plain) | `tool: bash {"command":"ls"}` |

Server inject tool descriptions vào system prompt và parse XML từ response.
Nếu model output chứa tool call XML không hợp lệ, server gửi lỗi text về để
model tự sửa.

## Cấu trúc thư mục

```
deepapi/
├── server.py              # Entry point (Flask app)
├── config.py              # Config: accounts, models, API keys
├── routes.py              # API routes + request handling
├── token_manager.py       # Multi-account rotation + token management
├── deepseek_client.py     # DeepSeek API client (PoW, SSE, session)
├── sse_handler.py         # SSE chunk builders
├── stream_handler.py      # Stream generators (auto-continue, retry)
├── tool_parser.py         # XML tool call parser (28+ formats)
├── prompt_builder.py      # System prompt builder + tool injection
├── .env                   # Environment variables (accounts, keys)
├── requirements.txt       # Python dependencies
├── tests/                 # Unit tests
└── README.md              # This file
```

## Quản lý

```bash
# Systemd
sudo systemctl status deepapi      # Xem trạng thái
sudo systemctl restart deepapi     # Khởi động lại
sudo systemctl stop deepapi        # Dừng

# Logs
journalctl -u deepapi -f           # Xem logs realtime
journalctl -u deepapi --since "5 minutes ago"  # Logs 5 phút gần nhất

# Kiểm tra debug
curl http://localhost:5001/v1/models -H "Authorization: Bearer deepcode2026"
```

## Development

```bash
# Chạy test
python3 -c "import sys; sys.path.insert(0, '.'); exec(open('tests/...').read())"

# Kiểm tra syntax
python3 -c "import py_compile; py_compile.compile('tool_parser.py', doraise=True)"

# Debug account rotation
export DEBUG_AUTH=1
python3 server.py
```

## License

Dự án này chỉ dành cho **mục đích nghiên cứu và học tập**.

## Credits

- [lessweb/deepcode-cli](https://github.com/lessweb/deepcode-cli.git) — nguồn tham khảo
- [taitestgame/deepapi](https://github.com/taitestgame/deepapi.git) — nguồn tham khảo
- [opencode](https://opencode.ai) — ứng dụng CLI agent
