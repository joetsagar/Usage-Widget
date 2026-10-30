"""Looks through recent Claude Code / Codex actions for things IT security would worry about.

Reads the same local logs as usage_details (~/.claude/projects, ~/.codex/sessions)
and checks the commands run, files written and connectors used - e.g. work data
sent to personal Gmail/Drive, passwords in commands, security settings turned off.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from usage_details import HOME, parse_ts, read_lines, recent_files, short

SHELL_TOOLS = {"Bash", "PowerShell", "shell", "exec_command", "local_shell"}
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "apply_patch"}

# (pattern, what it means) - checked against shell commands
COMMAND_RISKS = [
    (r"Set-MpPreference\s+.*-Disable|Add-MpPreference\s+.*-Exclusion", "Changed Windows Defender settings"),
    (r"netsh\s+advfirewall\s+.*\boff\b|Set-NetFirewallProfile\s+.*-Enabled\s+False", "Turned the firewall off"),
    (r"Set-ExecutionPolicy\s+(-\w+\s+)*(Bypass|Unrestricted)", "Turned off PowerShell script protection"),
    (r"sslVerify\s+false|--insecure\b|verify\s*=\s*False|NODE_TLS_REJECT_UNAUTHORIZED", "Switched off HTTPS certificate checks"),
    (r"(curl|wget|iwr|Invoke-WebRequest|irm|Invoke-RestMethod)\b[^|\n]*\|\s*(sh|bash|iex|Invoke-Expression|python)",
     "Downloaded a script from the internet and ran it straight away"),
    (r"(curl\b.*(-T|--upload-file|-F\s*\S*@|--data-binary\s*@))|(Invoke-\w+\s+.*-InFile)", "Uploaded a file to a website"),
    (r"pastebin\.com|transfer\.sh|file\.io|wetransfer|ngrok", "Used a file-sharing / tunnelling site"),
    (r"git\s+add\b[^\n]*\.(xlsx|xlsm|csv)\b", "Added a spreadsheet / CSV to git - check it has no work data before pushing"),
    (r"git\s+push\b[^\n]*--force|git\s+push\b[^\n]*\s-f\b", "Force-pushed to GitHub"),
    (r"\.ssh[\\/]id_|\.credentials\.json|auth\.json|\.env\b", "Read or touched a credentials file"),
    (r"(--password[ =]|-Password\s+|\bpassword\s*=\s*|\bpwd\s*=\s*)['\"]?[^\s'\"$%(]{6,}", "Typed a password into a command"),
]

# Secrets that should never appear in a command or a file Claude writes
SECRET_RISKS = [
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "an Anthropic API key"),
    (r"sk-[A-Za-z0-9]{32,}", "an API key"),
    (r"gh[pousr]_[A-Za-z0-9]{36}", "a GitHub token"),
    (r"AKIA[0-9A-Z]{16}", "an AWS key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
]

# Connectors that send data somewhere outside the work network
CONNECTOR_RISKS = [
    (r"Gmail__(send_message|forward|reply)", "Sent an email from personal Gmail"),
    (r"Google_Drive__(create_file|update_file|copy_file)", "Saved a file to personal Google Drive"),
    (r"Google_Drive__share_file", "Shared a personal Google Drive file"),
]


def check_tool(name, args):
    """Return the risks for one tool call."""
    found = []
    text = json.dumps(args) if not isinstance(args, str) else args
    if isinstance(args, dict) and args.get("dangerouslyDisableSandbox"):
        found.append("Ran a command with the sandbox switched off")
    for pattern, what in CONNECTOR_RISKS:
        if re.search(pattern, name):
            found.append(what)
    if name in SHELL_TOOLS:
        command = args.get("command", text) if isinstance(args, dict) else text
        command = " ".join(command) if isinstance(command, list) else str(command)
        for pattern, what in COMMAND_RISKS:
            if re.search(pattern, command, re.IGNORECASE):
                found.append(what)
    if name in SHELL_TOOLS or name in WRITE_TOOLS:
        for pattern, what in SECRET_RISKS:
            if re.search(pattern, text):
                found.append(f"Put {what} in plain text")
    return found


def scan(since):
    """Return findings newest first: {key, when, tool, chat, risk, detail}."""
    findings = []

    def add(when, sid, tool_id, name, args, chat):
        for risk in check_tool(name, args):
            detail = (args.get("command") or args.get("file_path") or "") if isinstance(args, dict) else args
            findings.append({"key": f"{sid}:{tool_id}:{risk}", "when": when, "tool": name, "chat": short(chat),
                             "risk": risk, "detail": short(str(detail), 70)})

    for path in recent_files(HOME / ".claude" / "projects", "**/*.jsonl", since):
        titles = {}
        for d in read_lines(path):
            sid = d.get("sessionId") or path.stem
            if d.get("type") == "ai-title":
                titles[sid] = d.get("aiTitle")
            if d.get("type") != "assistant" or not d.get("timestamp"):
                continue
            when = parse_ts(d["timestamp"])
            if when < since:
                continue
            for c in (d.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    add(when, sid, c.get("id"), c.get("name", ""), c.get("input") or {},
                        titles.get(sid) or chat_name(d.get("cwd")))

    for path in recent_files(HOME / ".codex" / "sessions", "**/*.jsonl", since):
        title = ""
        for d in read_lines(path):
            p = d.get("payload") or {}
            if p.get("type") == "user_message" and not title:
                title = p.get("message", "")
            if p.get("type") not in ("function_call", "custom_tool_call", "local_shell_call") or not d.get("timestamp"):
                continue
            when = parse_ts(d["timestamp"])
            if when < since:
                continue
            args = p.get("arguments") or p.get("input") or p.get("action") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            add(when, path.stem, p.get("call_id") or p.get("id"), p.get("name") or "shell", args,
                title or "Codex chat")

    findings.sort(key=lambda f: f["when"], reverse=True)
    return findings


def chat_name(cwd):
    return re.split(r"[\\/]", cwd or "")[-1] or "Claude chat"


def recent(days=1):
    return scan(datetime.now(timezone.utc) - timedelta(days=days))
