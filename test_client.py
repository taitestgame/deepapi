import sys
import os

# Force UTF-8 encoding for stdout and stderr on Windows to avoid UnicodeEncodeError
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

from deepseek_client import login, create_session, collect_response, delete_session

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

EMAIL    = os.environ.get("DEEPSEEK_EMAIL", "")
PASSWORD = os.environ.get("DEEPSEEK_PASSWORD", "")

def test():
    print("=== TEST DEEPSEEK CLIENT ===\n")

    # 1. Login
    print("[1] Đang login...")
    token = login(email=EMAIL, password=PASSWORD)
    print(f"    Token: {token[:30]}...\n")

    # 2. Tạo session
    print("[2] Tạo session...")
    session_id = create_session(token)
    print(f"    Session ID: {session_id}\n")

    # 3. Gửi message
    print("[3] Gửi message: 'Xin chào! Bạn là ai?'")
    result = collect_response(
        token=token,
        session_id=session_id,
        prompt="Human: Xin chào! Bạn là ai?\n\nAssistant:",
        model="deepseek-v4-flash",
        thinking=False,
    )

    print(f"\n=== KẾT QUẢ ===")
    print(f"Text: {result['text']}")
    if result.get('thinking'):
        print(f"Thinking: {result['thinking'][:100]}...")
    print(f"Finish reason: {result['finish_reason']}")

    # 4. Giữ lại session (không xóa)
    # delete_session(token, session_id)
    print("\n[4] Session được giữ lại. Done!")

if __name__ == "__main__":
    test()
