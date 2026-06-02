"""
JSON-based database for the bot.
Stores EP records, event logs, and verification data.
Thread-safe via asyncio lock.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("database")

DB_PATH = os.getenv("DB_PATH", "bot_data.json")

_lock = asyncio.Lock()

_DEFAULT: dict = {
    "ep_records":            {},   # roblox_username.lower() -> {username, ep, discord_id, joined_at, last_updated}
    "event_log":             [],   # list of event entry dicts
    "ep_audit_log":          [],   # every EP change ever
    "verified_users":        {},   # discord_id (str) -> roblox_username
    "pending_verifications": {},   # discord_id (str) -> {code, roblox_username, expires_at}
    "promotion_log":         [],   # auto-promotion records
    "rank_log":              [],   # manual rank-change records
    "warnings":              {},   # discord_id (str) -> list of warning dicts
}


# ── Low-level helpers (synchronous, call within _lock) ────────────────────────

def _load_raw() -> dict:
    if not os.path.exists(DB_PATH):
        return {k: (type(v)() if not isinstance(v, dict) else {}) for k, v in _DEFAULT.items()}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        log.exception("Database corrupted, rebuilding.")
        data = _DEFAULT.copy()
    # Back-fill missing top-level keys
    for k, v in _DEFAULT.items():
        if k not in data:
            data[k] = type(v)()
    return data


def _save_raw(data: dict):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, DB_PATH)


# ── Public async helpers ──────────────────────────────────────────────────────

async def load() -> dict:
    async with _lock:
        return _load_raw()


async def save(data: dict):
    async with _lock:
        _save_raw(data)


# ── EP helpers ────────────────────────────────────────────────────────────────

async def get_ep(roblox_username: str) -> Optional[dict]:
    data = await load()
    return data["ep_records"].get(roblox_username.lower())


async def set_ep(
    roblox_username: str,
    ep_delta:        int,
    editor_discord_id: int,
    note:            str = "",
) -> dict:
    """
    Add ep_delta (may be negative) to a user's EP total. Creates the record if missing.
    EP is floored at 0.
    Returns the updated record dict.
    """
    async with _lock:
        data = _load_raw()
        key  = roblox_username.lower()
        rec  = data["ep_records"].get(key)
        if rec is None:
            rec = {
                "username":     roblox_username,
                "ep":           0,
                "discord_id":   None,
                "joined_at":    datetime.now(timezone.utc).isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            data["ep_records"][key] = rec

        old_ep    = rec["ep"]
        rec["ep"] = max(0, old_ep + ep_delta)
        rec["last_updated"] = datetime.now(timezone.utc).isoformat()

        data["ep_audit_log"].append({
            "roblox_username":   roblox_username,
            "old_ep":            old_ep,
            "new_ep":            rec["ep"],
            "delta":             rec["ep"] - old_ep,  # actual delta after floor
            "editor_discord_id": editor_discord_id,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
            "note":              note,
        })
        # Keep audit log bounded
        data["ep_audit_log"] = data["ep_audit_log"][-10_000:]

        _save_raw(data)
        return dict(rec)


async def set_ep_absolute(
    roblox_username:   str,
    ep_amount:         int,
    editor_discord_id: int,
    note:              str = "",
) -> dict:
    """Set EP to an absolute value. Returns the updated record including old_ep."""
    async with _lock:
        data = _load_raw()
        key  = roblox_username.lower()
        rec  = data["ep_records"].get(key)
        if rec is None:
            rec = {
                "username":     roblox_username,
                "ep":           0,
                "discord_id":   None,
                "joined_at":    datetime.now(timezone.utc).isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            data["ep_records"][key] = rec

        old_ep    = rec["ep"]
        rec["ep"] = max(0, ep_amount)
        rec["last_updated"] = datetime.now(timezone.utc).isoformat()

        data["ep_audit_log"].append({
            "roblox_username":   roblox_username,
            "old_ep":            old_ep,
            "new_ep":            rec["ep"],
            "delta":             rec["ep"] - old_ep,
            "editor_discord_id": editor_discord_id,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
            "note":              note,
        })
        data["ep_audit_log"] = data["ep_audit_log"][-10_000:]

        _save_raw(data)
        return {**dict(rec), "old_ep": old_ep}


async def get_leaderboard(limit: int = 20) -> list:
    data    = await load()
    records = list(data["ep_records"].values())
    records.sort(key=lambda r: r.get("ep", 0), reverse=True)
    return records[:limit]


async def get_all_ep_records() -> dict:
    data = await load()
    return data["ep_records"]


# ── Event log ─────────────────────────────────────────────────────────────────

async def log_event(
    event_type:      str,
    ep_awarded:      int,
    participants:    list,
    host_discord_id: int,
    host_name:       str,
    note:            str = "",
) -> dict:
    async with _lock:
        data  = _load_raw()
        entry = {
            "event_type":       event_type,
            "ep_awarded":       ep_awarded,
            "participants":     participants,
            "participant_count": len(participants),
            "host_discord_id":  host_discord_id,
            "host_name":        host_name,
            "note":             note,
            "logged_at":        datetime.now(timezone.utc).isoformat(),
        }
        data["event_log"].append(entry)
        _save_raw(data)
        return entry


async def get_recent_events(limit: int = 50) -> list:
    data = await load()
    return list(reversed(data["event_log"][-limit:]))


async def get_events_this_week() -> list:
    """Return all events logged since last Sunday midnight UTC."""
    from datetime import timedelta
    data = await load()
    now  = datetime.now(timezone.utc)

    days_since_sunday = (now.weekday() + 1) % 7  # Mon=1…Sun=0
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_sunday)

    result = []
    for ev in data["event_log"]:
        try:
            ts = datetime.fromisoformat(ev["logged_at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= week_start:
                result.append(ev)
        except Exception:
            pass
    return result


# ── Verification ──────────────────────────────────────────────────────────────

async def is_roblox_verified(roblox_username: str) -> bool:
    data   = await load()
    target = roblox_username.lower()
    return any(u.lower() == target for u in data["verified_users"].values())


async def store_pending_verification(
    discord_id: int, roblox_username: str, code: str, expires_at: str
):
    async with _lock:
        data = _load_raw()
        data["pending_verifications"][str(discord_id)] = {
            "roblox_username": roblox_username,
            "code":            code,
            "expires_at":      expires_at,
        }
        _save_raw(data)


async def get_pending_verification(discord_id: int) -> Optional[dict]:
    data = await load()
    return data["pending_verifications"].get(str(discord_id))


async def get_roblox_username(discord_id: int) -> Optional[str]:
    data = await load()
    return data["verified_users"].get(str(discord_id))


async def get_discord_id_for_roblox(roblox_username: str) -> Optional[int]:
    data = await load()
    for did, rbx in data["verified_users"].items():
        if rbx.lower() == roblox_username.lower():
            return int(did)
    return None


async def claim_verification(discord_id: int, roblox_username: str) -> bool:
    """
    Link discord_id ↔ roblox_username.
    Returns False if roblox_username is already claimed by a *different* Discord account.
    """
    async with _lock:
        data   = _load_raw()
        target = roblox_username.lower()

        # Check for conflict
        for did, username in data["verified_users"].items():
            if username.lower() == target and int(did) != discord_id:
                return False

        data["verified_users"][str(discord_id)] = roblox_username
        data["pending_verifications"].pop(str(discord_id), None)

        # Link discord_id to EP record if it exists
        key = target
        if key in data["ep_records"]:
            data["ep_records"][key]["discord_id"] = discord_id

        _save_raw(data)
        return True


async def remove_verification(discord_id: int):
    async with _lock:
        data          = _load_raw()
        roblox_username = data["verified_users"].pop(str(discord_id), None)
        data["pending_verifications"].pop(str(discord_id), None)

        if roblox_username:
            key = roblox_username.lower()
            if key in data["ep_records"]:
                data["ep_records"][key]["discord_id"] = None

        _save_raw(data)


# ── Promotion / rank logs ──────────────────────────────────────────────────────

async def log_promotion(
    roblox_username: str,
    old_rank:        int,
    new_rank:        int,
    ep:              int,
):
    async with _lock:
        data = _load_raw()
        data["promotion_log"].append({
            "roblox_username": roblox_username,
            "old_rank":        old_rank,
            "new_rank":        new_rank,
            "ep":              ep,
            "promoted_at":     datetime.now(timezone.utc).isoformat(),
        })
        _save_raw(data)


async def log_rank_change(
    roblox_username: str,
    old_rank:        int,
    new_rank:        int,
    editor_id:       int,
):
    async with _lock:
        data = _load_raw()
        data["rank_log"].append({
            "roblox_username": roblox_username,
            "old_rank":        old_rank,
            "new_rank":        new_rank,
            "editor_id":       editor_id,
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        })
        _save_raw(data)