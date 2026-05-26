"""
Test nhanh DeepSeek client
Chạy: python test_client.py
"""

from deepseek_client import login, create_session, collect_response, delete_session

EMAIL    = ""
PASSWORD = ""

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
