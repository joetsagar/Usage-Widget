"""Works out where Claude / Codex usage went this week and what to change.

Claude Code logs token counts per message (~/.claude/projects), so Claude
shares are estimates weighted by price. Codex logs the actual limit % after
every turn (~/.codex/sessions), so Codex figures are real % of the limit.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()

# Relative price per token type (input = 1) and per model, used to weight
# Claude token counts into something close to how limits are charged.
TOKEN_WEIGHTS = {"input": 1, "cache_5m": 1.25, "cache_1h": 2, "cache_read": 0.1, "output": 5}
MODEL_WEIGHTS = {"opus": 1.0, "sonnet": 0.6, "haiku": 0.2, "fable": 1.0}


def parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def short(text, n=34):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def model_family(model):
    model = (model or "").lower()
    for name in MODEL_WEIGHTS:
        if name in model:
            return name.title()
    return model or "Unknown"


def recent_files(pattern_root, pattern, since):
    cutoff = since.timestamp()
    for path in pattern_root.glob(pattern):
        try:
            if path.stat().st_mtime >= cutoff:
                yield path
        except OSError:
            pass


def read_lines(path):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass
    except OSError:
        return


# --- Claude ---------------------------------------------------------------
def analyse_claude(since):
    sessions = defaultdict(lambda: {"weight": 0.0, "turns": 0, "context": 0, "project": "", "titles": {}})
    models = Counter()
    agents = 0.0
    seen = set()

    for path in recent_files(HOME / ".claude" / "projects", "**/*.jsonl", since):
        for d in read_lines(path):
            sid = d.get("sessionId")
            if not sid:
                continue
            s = sessions[sid]
            kind = d.get("type")
            if kind == "custom-title":
                s["titles"]["custom"] = d.get("customTitle")
            elif kind == "ai-title":
                s["titles"]["ai"] = d.get("aiTitle")
            elif kind == "user" and "first" not in s["titles"] and not d.get("isSidechain"):
                content = (d.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                    s["titles"]["first"] = content
            elif kind == "assistant":
                msg = d.get("message") or {}
                usage = msg.get("usage")
                model = msg.get("model", "")
                if not usage or model.startswith("<") or msg.get("id") in seen:
                    continue
                if not d.get("timestamp") or parse_ts(d["timestamp"]) < since:
                    continue
                seen.add(msg.get("id"))
                cache = usage.get("cache_creation") or {}
                c1h = cache.get("ephemeral_1h_input_tokens", 0)
                c5m = usage.get("cache_creation_input_tokens", 0) - c1h
                tokens = (
                    usage.get("input_tokens", 0) * TOKEN_WEIGHTS["input"]
                    + c5m * TOKEN_WEIGHTS["cache_5m"]
                    + c1h * TOKEN_WEIGHTS["cache_1h"]
                    + usage.get("cache_read_input_tokens", 0) * TOKEN_WEIGHTS["cache_read"]
                    + usage.get("output_tokens", 0) * TOKEN_WEIGHTS["output"]
                )
                family = model_family(model)
                weight = tokens * MODEL_WEIGHTS.get(family.lower(), 1.0)
                s["weight"] += weight
                s["project"] = Path(d.get("cwd") or "").name
                models[family] += weight
                if d.get("isSidechain"):
                    agents += weight
                else:
                    s["turns"] += 1
                    s["context"] += (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                                     + usage.get("cache_read_input_tokens", 0))

    total = sum(s["weight"] for s in sessions.values())
    if not total:
        return None
    chats = []
    for s in sessions.values():
        if s["weight"]:
            t = s["titles"]
            title = t.get("custom") or t.get("ai") or t.get("first") or "Untitled chat"
            avg_ctx = s["context"] / s["turns"] if s["turns"] else 0
            chats.append({"title": short(title), "share": s["weight"] / total * 100, "avg_context": avg_ctx,
                          "turns": s["turns"]})
    chats.sort(key=lambda c: c["share"], reverse=True)
    return {
        "chats": chats,
        "models": {m: w / total * 100 for m, w in models.most_common()},
        "agents_share": agents / total * 100,
        "unit": "of Claude Code use",
    }


# --- Codex ----------------------------------------------------------------
def analyse_codex(since, weekly_reset):
    events = []  # (time, session, pct, reset_at, model, effort, context)
    titles = {}
    for path in recent_files(HOME / ".codex" / "sessions", "**/*.jsonl", since):
        sid, model, effort = path.stem, "", ""
        for d in read_lines(path):
            p = d.get("payload") or {}
            if d.get("type") == "session_meta" and p.get("timestamp"):
                titles.setdefault(sid + "#start", parse_ts(p["timestamp"]).astimezone())
            elif d.get("type") == "turn_context":
                model, effort = p.get("model", ""), p.get("effort", "")
            elif p.get("type") == "user_message" and sid not in titles:
                titles[sid] = p.get("message", "")
            elif p.get("type") == "token_count":
                week = ((p.get("rate_limits") or {}).get("secondary")) or {}
                if "used_percent" not in week or not d.get("timestamp"):
                    continue
                ctx = (((p.get("info") or {}).get("last_token_usage")) or {}).get("input_tokens", 0)
                events.append((parse_ts(d["timestamp"]), sid, week["used_percent"], week.get("resets_at"),
                               model, effort, ctx))

    reset_ts = int(weekly_reset.timestamp()) if weekly_reset else None
    events = [e for e in events if e[0] >= since and (reset_ts is None or abs((e[3] or 0) - reset_ts) < 120)]
    if not events:
        return None
    events.sort()

    per_chat, per_model, per_effort = Counter(), Counter(), Counter()
    ctx_sum, ctx_n = Counter(), Counter()
    last = 0.0
    for _, sid, pct, _, model, effort, ctx in events:
        delta = max(pct - last, 0)
        last = max(last, pct)
        per_chat[sid] += delta
        per_model[model or "Unknown"] += delta
        per_effort[effort or "default"] += delta
        ctx_sum[sid] += ctx
        ctx_n[sid] += 1

    def codex_title(sid):
        if titles.get(sid):
            return short(titles[sid])
        start = titles.get(sid + "#start")
        return f"Chat from {start:%a %H:%M}" if start else "Untitled chat"

    chats = [{"title": codex_title(sid), "share": pct,
              "avg_context": ctx_sum[sid] / ctx_n[sid], "turns": ctx_n[sid]}
             for sid, pct in per_chat.most_common() if pct > 0]
    return {
        "chats": chats,
        "models": dict(per_model.most_common()),
        "efforts": dict(per_effort.most_common()),
        "logged_total": last,
        "unit": "of weekly limit",
    }


# --- pace and recommendations ---------------------------------------------
def pace(pct, resets, window, now):
    """Return (text, will_run_out)."""
    if resets is None:
        return "", False
    if pct >= 100:
        return f"Limit reached – back {fmt_when(resets, now)}", True
    elapsed = (now - (resets - window)).total_seconds()
    if pct <= 0 or elapsed <= 0:
        return "Plenty left", False
    rate = pct / elapsed
    hit = now + timedelta(seconds=(100 - pct) / rate)
    if hit < resets and elapsed / window.total_seconds() > 0.03:
        return f"At this rate you'll run out {fmt_when(hit, now)}", True
    expected = elapsed / window.total_seconds() * 100
    return f"On track ({pct:.0f}% used, {expected:.0f}% of time gone)", False


def fmt_when(when, now):
    local = when.astimezone()
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == now.astimezone().date():
        return f"at {clock}"
    return f"{local:%a} {clock}"


def recommendations(name, rows, detail, other_rows, now):
    tips = []
    week = next((r for r in rows if "Weekly" in r[0]), None)
    session = next((r for r in rows if "Session" in r[0]), None)

    if week and week[2]:
        _, pct, resets, window = week
        days_left = max((resets - now).total_seconds() / 86400, 0.1)
        _, short_fall = pace(pct, resets, window, now)
        if short_fall and pct < 100:
            per_day = (100 - pct) / days_left
            tips.append(f"Weekly is ahead of pace. Keep to about {per_day:.0f}% a day until it resets "
                        f"{fmt_when(resets, now)}.")
    if session and session[1] >= 80 and session[2]:
        tips.append(f"Session nearly full – pause big jobs until it resets {fmt_when(session[2], now)}.")

    other_week = next((r for r in other_rows or [] if "Weekly" in r[0]), None)
    other = "Codex" if name == "Claude" else "Claude"
    if week and other_week and week[1] >= 70 and other_week[1] <= 60:
        tips.append(f"{other} has {100 - other_week[1]:.0f}% of its week left – move routine jobs "
                    f"(emails, comments, checklists) there.")

    if detail:
        chats = detail["chats"]
        if chats and chats[0]["share"] >= 25:
            top = chats[0]
            tips.append(f"\"{top['title']}\" used {top['share']:.0f}% {detail['unit']} on its own.")
        big = [c for c in chats[:5] if c["avg_context"] > 120_000]
        if big:
            tips.append(f"\"{big[0]['title']}\" averaged {big[0]['avg_context'] / 1000:.0f}k tokens per message. "
                        f"Long chats re-send everything each time – start a new chat (or /clear) between tasks.")
        if name == "Claude":
            opus = detail["models"].get("Opus", 0) + detail["models"].get("Fable", 0)
            if opus >= 60:
                tips.append(f"Opus did {opus:.0f}% of the work. Use /model sonnet for routine jobs and "
                            f"save Opus for investigations.")
            if detail["agents_share"] >= 15:
                tips.append(f"Sub-agents used {detail['agents_share']:.0f}%. Ask Claude to do it inline rather "
                            f"than spawn agents.")
        else:
            heavy = sum(v for k, v in detail.get("efforts", {}).items() if k in ("high", "xhigh"))
            total = sum(detail.get("efforts", {}).values()) or 1
            if heavy / total >= 0.5:
                tips.append(f"{heavy / total * 100:.0f}% of Codex use was on high reasoning. Drop to medium "
                            f"for simple tasks (/model to change).")
            if week and week[1] - detail["logged_total"] >= 10:
                tips.append(f"About {week[1] - detail['logged_total']:.0f}% was used outside this PC "
                            f"(ChatGPT app / cloud tasks).")
    if not tips:
        tips.append("Nothing to change – you're on track.")
    return tips


def build(name, rows, other_rows):
    """Everything the detail panel shows for one service."""
    now = datetime.now(timezone.utc)
    week = next((r for r in rows if "Weekly" in r[0]), None)
    since = (week[2] - week[3]) if week and week[2] else now - timedelta(days=7)
    if name == "Claude":
        detail = analyse_claude(since)
    else:
        detail = analyse_codex(since, week[2] if week else None)
    return {
        "pace": [(label, pace(pct, resets, window, now)[0]) for label, pct, resets, window in rows],
        "detail": detail,
        "tips": recommendations(name, rows, detail, other_rows, now),
    }
