"""Read Kilo local usage without changing its database or login."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "kilo"


def fetch(get_json):
    result = {"cost": 0.0, "tokens": 0, "chats": []}
    database = DATA_DIR / "kilo.db"
    if not database.exists():
        raise RuntimeError("Open Kilo Code to start logging usage")
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        chats = {}
        for title, raw in conn.execute(
            "SELECT s.title, m.data FROM message m JOIN session s ON s.id=m.session_id "
            "WHERE m.time_created >= ?", (cutoff,)
        ):
            message = json.loads(raw)
            if message.get("role") != "assistant":
                continue
            cost = float(message.get("cost") or 0)
            tokens = message.get("tokens") or {}
            total = tokens.get("total")
            if total is None:
                cache = tokens.get("cache") or {}
                total = sum(tokens.get(k, 0) for k in ("input", "output", "reasoning")) + sum(cache.values())
            result["cost"] += cost
            result["tokens"] += total
            chats[title] = chats.get(title, 0) + cost
        result["chats"] = sorted(chats.items(), key=lambda item: item[1], reverse=True)
    return result


def summary(data):
    return [("Tokens (7 days)", f"{data['tokens']:,}")]
