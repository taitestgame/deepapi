"""
DeepSeek API Server - Prompt Builder
"""
import json
from config import VALID_TOOLS
import re
from tool_parser import _get_valid_tool_set, _extract_tool_calls_safe
XML_TOOL_INSTRUCTION = (
    "\n\n**CRITICAL TOOL CALL FORMAT: To use a tool, output `<tool>NAME</tool>` followed by `<json>PARAMS</json>`. "
    "Example: `<tool>bash</tool><json>{\"command\":\"ls\"}</json>`. The tool name inside `<tool>` tag is REQUIRED. No text before or after.**"
    "\n**VALID TOOLS ONLY: " + ", ".join(sorted(VALID_TOOLS)) + ". Any other tool name = ERROR.**"
    "\n**WARNING: Do NOT output tool names copied from code examples you read (like 'name', 'NAME', 'N', 'X', 'tool_name', 'TÊN'). "
    "Only use tools from the Valid tools list above. If you need to explain code, use plain text, NOT tool calls.**"
)
def _build_example_args(func: dict) -> dict:
    params = func.get("parameters", {}).get("properties", {})
    required = func.get("parameters", {}).get("required", [])
    args = {}
    for pname, pinfo in params.items():
        ptype = pinfo.get("type", "string")
        enum = pinfo.get("enum", [])
        if ptype == "array":
            items = pinfo.get("items", {})
            item_props = items.get("properties", {})
            if item_props:
                item_required = items.get("required", [])
                ex = {}
                for ipname, ipinfo in item_props.items():
                    ienum = ipinfo.get("enum", [])
                    if ipname in item_required:
                        ex[ipname] = (ienum or [ipinfo.get("description", "...")])[0]
                    else:
                        ex[ipname] = "..."
                args[pname] = [ex]
            else:
                args[pname] = []
        elif ptype == "object":
            obj_props = pinfo.get("properties", {})
            if obj_props:
                args[pname] = {k: v.get("description", "...") for k, v in obj_props.items()}
            else:
                args[pname] = {}
        elif pname in required:
            args[pname] = (enum or [pinfo.get("description", "...")])[0]
        else:
            args[pname] = "..."
    return args

def _describe_param(pname: str, pinfo: dict, required: set, indent: int = 2) -> list:
    prefix = " " * indent
    lines = []
    ptype = pinfo.get("type", "string")
    enum = pinfo.get("enum", [])
    req_mark = " (required)" if pname in required else ""
    pdesc = pinfo.get("description", "")
    type_hint = f" [{ptype}]" if not enum else f" [{ptype}, enum: {'|'.join(enum)}]"
    lines.append(f"{prefix}- {pname}:{type_hint} {pdesc}{req_mark}")
    if ptype == "array":
        items = pinfo.get("items", {})
        item_props = items.get("properties", {})
        if item_props:
            item_required = set(items.get("required", []))
            lines.append(f"{prefix}  items:")
            for ipname, ipinfo in item_props.items():
                lines.extend(_describe_param(ipname, ipinfo, item_required, indent + 4))
    elif ptype == "object":
        obj_props = pinfo.get("properties", {})
        if obj_props:
            obj_required = set(pinfo.get("required", []))
            lines.append(f"{prefix}  properties:")
            for opname, opinfo in obj_props.items():
                lines.extend(_describe_param(opname, opinfo, obj_required, indent + 4))
    return lines

def _build_tool_system_prompt(tools: list) -> str:
    """Build system prompt fragment describing available tools in XML format."""
    if not tools:
        return ""

    lines = ["## Available Tools"]
    lines.append("To use a tool, output EXACTLY:")

    first = tools[0].get("function", {})
    example_name = first.get("name", "tool_name")
    example_args = _build_example_args(first)
    lines.append(f"<tool>{example_name}</tool>")
    lines.append("<json>")
    lines.append(json.dumps(example_args, indent=2, ensure_ascii=False))
    lines.append("</json>")
    if example_args:
        flat = []
        for k, v in example_args.items():
            if isinstance(v, (list, dict)):
                flat.append(f"<{k}>...</{k}>")
            else:
                flat.append(f"<{k}>{v}</{k}>")
        lines.append(f"Or: <tool>{example_name}</tool>{''.join(flat)}")
    lines.append("")
    lines.append("CRITICAL: Only use tools listed below. Any other tool name will cause an error.")
    lines.append("Valid tool names: " + ", ".join(sorted(VALID_TOOLS)))
    lines.append("")
    lines.append("WARNING: Do NOT output tool names copied from code examples you read")
    lines.append("(like 'name', 'NAME', 'N', 'X', 'tool_name', 'TEN').")
    lines.append("If you need to explain code, use plain text, NOT tool calls.")
    lines.append("")
    lines.append("Available tools:")

    for t in tools:
        func = t.get("function", {})
        name = func.get("name", "unknown")
        desc = func.get("description", "")
        params = func.get("parameters", {}).get("properties", {})
        required = set(func.get("parameters", {}).get("required", []))

        lines.append(f"\n### {name}")
        lines.append(f"Description: {desc}")
        if params:
            lines.append("Parameters:")
            for pname, pinfo in params.items():
                lines.extend(_describe_param(pname, pinfo, required))

    return "\n".join(lines)

def _has_xml_tools(messages: list) -> bool:
    xml_tool_pattern = re.compile(r'<(write|bash|read|edit|glob|grep|task|todowrite|webfetch|websearch|ask|AskUserQuestion|WebSearch|UpdatePlan)>', re.IGNORECASE)
    for msg in messages:
        if msg.get('role') == 'system':
            content = msg.get('content', '')
            if isinstance(content, list):
                content = chr(10).join(item.get('text', '') for item in content if isinstance(item, dict))
            if xml_tool_pattern.search(str(content)):
                return True
    return False

def build_prompt(messages: list, tools: list = None) -> str:
    """Build a text prompt from OpenAI-format messages, with optional tool support."""
    parts = []
    tool_prompt_inserted = False
    has_explicit_tools = bool(tools)
    has_implicit_tools = not has_explicit_tools and _has_xml_tools(messages)
    has_tools = has_explicit_tools or has_implicit_tools

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
            content = str(content).replace("If none of the available skills match, respond with an empty array, i.e. `{\"skillNames\": []}`.", "Neu khong co skill phu hop, hay tra loi tu nhien bang tieng Viet.")
            content = str(content).replace('Response in JSON format:\n```\n{\n  "skillNames": ["", ...]\n}\n```','')
            if has_implicit_tools and not tool_prompt_inserted:
                content = content + XML_TOOL_INSTRUCTION
                tool_prompt_inserted = True
            parts.append(f"<system>\n{content}\n</system>")
            # Inject tool descriptions after the last system message (only if tools provided)
            if has_explicit_tools and not tool_prompt_inserted:
                tool_prompt = _build_tool_system_prompt(tools)
                if tool_prompt:
                    parts.append(f"<system>\n{tool_prompt}\n</system>")
                tool_prompt_inserted = True
        #elif role == "user":
           # parts.append(f"Human: {content}")
        elif role == "user":
            if has_explicit_tools and tools:
                ex_func = tools[0].get("function", {})
                ex_name = ex_func.get("name", "tool_name")
                ex_args = _build_example_args(ex_func)
                ex_json = json.dumps(ex_args, indent=2, ensure_ascii=False)
            else:
                ex_name = "tool_name"
                ex_json = '{"param1": "value1"}'
            content += f"""

            IMPORTANT:
            ## Available Tools
            To use a tool, output EXACTLY:
            <tool>{ex_name}</tool>
            <json>
            {ex_json}
            </json>

            Or nested format:
            <tool><{ex_name}>value</{ex_name}></tool>
            If no tool is needed, respond normally in Vietnamese.
            Tool calls must be sent in the response to the user, not written in thinking.
            """
            parts.append(f"Human: {content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    args_str = func.get("arguments", "{}")
                    if isinstance(args_str, dict):
                        args_str = json.dumps(args_str)
                    parts.append(
                        f'Assistant: <tool>{func.get("name", "")}</tool>\n'
                        f'<json>\n{args_str}\n</json>'
                    )
            else:
                parts.append(f"Assistant: {content}")
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            parts.append(f"Human: [Tool result for call {tool_call_id}]\n{content}")

    # Insert tool prompt at beginning if no system message existed
    if has_tools and not tool_prompt_inserted:
        tool_prompt = _build_tool_system_prompt(tools)
        if tool_prompt:
            parts.insert(0, f"<system>\n{tool_prompt}\n</system>")

    # If tools are present and no tool history in messages, hint the model to output XML
    has_tool_history = any(
        msg.get("role") == "tool" or
        (msg.get("role") == "assistant" and msg.get("tool_calls"))
        for msg in messages
    )
    parts.append("Assistant:")
    return "\n\n".join(parts)

	
