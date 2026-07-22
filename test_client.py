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
from server import build_prompt, parse_tool_calls_from_text

def test_tool_calling_logic():
    print("=== TEST OFFLINE TOOL CALLING LOGIC ===")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Đọc nội dung file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "Liệt kê thư mục",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}
            }
        }
    ]

    messages = [{"role": "user", "content": "Kiểm tra thư mục và đọc file README"}]
    prompt = build_prompt(messages, tools=tools)
    assert "[AVAILABLE TOOLS]" in prompt
    print("[PASSED] Prompt injection OK!")

    # Test Multiple Tool Calls in single response
    multi_output = """Tôi sẽ liệt kê thư mục và đọc file README.

```json_tool_call
{
  "name": "list_dir",
  "arguments": { "path": "./" }
}
```

```json_tool_call
{
  "name": "read_file",
  "arguments": { "path": "./README.md" }
}
```"""

    has_tool, tool_calls, clean_text = parse_tool_calls_from_text(multi_output)
    print(f"\n--- MULTIPLE TOOL CALLS TEST ---")
    print(f"Has Tool Call: {has_tool}")
    print(f"Tool Calls Count: {len(tool_calls)}")
    for tc in tool_calls:
        print(f"  - Tool: {tc['function']['name']}, Args: {tc['function']['arguments']}")

    assert has_tool is True
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "list_dir"
    assert tool_calls[1]["function"]["name"] == "read_file"
    print("[PASSED] Multiple Tool Calls parsing OK!")

def test():
    print("=== TEST DEEPSEEK CLIENT ===\n")
    test_tool_calling_logic()

if __name__ == "__main__":
    test()
