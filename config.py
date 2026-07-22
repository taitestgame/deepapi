"""
DeepSeek API Server - Configuration & Constants
"""

import sys
import os

# ── Force UTF-8 encoding on Windows ──
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass


def load_env():
    """Load .env file into os.environ (called before importing other modules)."""
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


# ── Load .env immediately at import time ──
load_env()

# ── API Keys ──
VALID_API_KEYS = {
    os.environ.get("API_KEY", "sk-my-secret-key-1"),
}

# ── DeepSeek Accounts ──
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

# ── Available Models ──
AVAILABLE_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-r1",
    "deepseek-v3",
    # Qwen aliases
    "qwen-plus",
    "qwen-max",
    "qwen-turbo",
    "qwen2.5-coder-32b-instruct",
    "qwen2.5-72b-instruct",
]

# ── Model Aliases ──
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

# ── Valid Tools (used by tool parser + validation + prompt builder) ──
VALID_TOOLS = {
    'bash',
    'read',
    'write',
    'edit',
    'glob',
    'grep',
    'task',
    'skill',
    'lsp',
    'todowrite',
    'webfetch',
    'websearch',
    'apply_patch',
    'ask',
    'AskUserQuestion',
    'UpdatePlan',
    'WebSearch',
    'web_search',
    'python',
}

# ── Debug Logging ──
DEBUG_LOG_PATH = "/tmp/cli_debug.log"
DEBUG_LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
DEBUG_LOG_KEEP = 1 * 1024 * 1024      # giữ lại 1MB cuối


# --- resolve_model (from server.py) ---
def resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model.strip().lower(), model.strip())

