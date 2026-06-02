"""
JSON-based database for the bot. Stores EP records, event logs, and verification data.
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

_DEFAULT = {
    "ep_records": {},         # roblox_username (lower) -> {username, ep, discord_id, joined_at, last_updated}
    "event_log": [],          # list of event dicts
    "ep_audit_log": [],       # every EP change ever
    "verified_users": {},     # discord_id (str) -> roblox_username
    "pending_verifications": {},  # discord_id -> {code, roblox_username, expires_at}
    "promotion_log": [],      # auto-promotion records
}


def _load_raw() -> dict:
    if not os.path.exists(DB_PATH):
        return dict(_DEFAULT)
    with open(DB_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Back-fill any missing top-level keys
    for k, v in _DEFAULT.items():
        if k not in data:
            data[k] = type(v)()
    return data


def _save_raw(data: dict):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, DB_PATH)


async def load() -> dict:
    async with _lock:
        return _load_raw()


async def save(data: dict):
    async with _lock:
        _save_raw(data)


# ── EP helpers ──────────────────────────────────────────────────────────────

async def get_ep(roblox_username: str) -> Optional[dict]:
    data = await load()
    return data["ep_records"].get(roblox_username.lower())


async def set_ep(roblox_username: str, ep_delta: int, editor_discord_id: int, note: str = "") -> dict:
    """Add ep_delta (can be negative) to a user's EP. Creates record if missing."""
    async with _lock:
        data = _load_raw()
        key = roblox_username.lower()
        rec = data["ep_records"].get(key)
        if rec is None:
            rec = {
                "username": roblox_username,
                "ep": 0,
                "discord_id": None,
                "joined_at": datetime.now(timezone.utc).isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            data["ep_records"][key] = rec
        old_ep = rec["ep"]
        rec["ep"] = max(0, old_ep + ep_delta)
        rec["last_updated"] = datetime.now(timezone.utc).isoformat()
        # Audit
        data["ep_audit_log"].append({
            "roblox_username": roblox_username,
            "old_ep": old_ep,
            "new_ep": rec["ep"],
            "delta": ep_delta,
            "editor_discord_id": editor_discord_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "note": note,
        })
        _save_raw(data)
        return dict(rec)


async def get_leaderboard(limit: int = 20) -> list:
    data = await load()
    records = list(data["ep_records"].values())
    records.sort(key=lambda r: r["ep"], reverse=True)
    return records[:limit]


async def get_all_ep_records() -> dict:
    data = await load()
    return data["ep_records"]


# ── Event log ────────────────────────────────────────────────────────────────

async def log_event(event_type: str, ep_awarded: int, participants: list,
                    host_discord_id: int, host_name: str, note: str = "") -> dict:
    async with _lock:
        data = _load_raw()
        entry = {
            "event_type": event_type,
            "ep_awarded": ep_awarded,
            "participants": participants,
            "participant_count": len(participants),
            "host_discord_id": host_discord_id,
            "host_name": host_name,
            "note": note,
            "logged_at": datetime.now(timezone.utc).isoformat(),
        }
        data["event_log"].append(entry)
        _save_raw(data)
        return entry


async def get_recent_events(limit: int = 50) -> list:
    data = await load()
    return list(reversed(data["event_log"][-limit:]))


async def get_events_this_week() -> list:
    """Events since last Sunday midnight UTC."""
    data = await load()
    now = datetime.now(timezone.utc)
    # Days since Sunday
    days_since_sunday = now.weekday() + 1 if now.weekday() != 6 else 0
    week_start = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    from datetime import timedelta
    week_start -= timedelta(days=days_since_sunday)

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


# ── Verification ─────────────────────────────────────────────────────────────

async def store_pending_verification(discord_id: int, roblox_username: str, code: str, expires_at: str):
    async with _lock:
        data = _load_raw()
        data["pending_verifications"][str(discord_id)] = {
            "roblox_username": roblox_username,
            "code": code,
            "expires_at": expires_at,
        }
        _save_raw(data)


async def get_pending_verification(discord_id: int) -> Optional[dict]:
    data = await load()
    return data["pending_verifications"].get(str(discord_id))


async def confirm_verification(discord_id: int, roblox_username: str):
    async with _lock:
        data = _load_raw()
        data["verified_users"][str(discord_id)] = roblox_username
        data["pending_verifications"].pop(str(discord_id), None)
        # Link discord_id to EP record if it exists
        key = roblox_username.lower()
        if key in data["ep_records"]:
            data["ep_records"][key]["discord_id"] = discord_id
        _save_raw(data)


async def get_roblox_username(discord_id: int) -> Optional[str]:
    data = await load()
    return data["verified_users"].get(str(discord_id))


async def get_discord_id_for_roblox(roblox_username: str) -> Optional[int]:
    data = await load()
    for did, rbx in data["verified_users"].items():
        if rbx.lower() == roblox_username.lower():
            return int(did)
    return None


# ── Promotion log ─────────────────────────────────────────────────────────────

async def log_promotion(roblox_username: str, old_rank: int, new_rank: int, ep: int):
    async with _lock:
        data = _load_raw()
        data["promotion_log"].append({
            "roblox_username": roblox_username,
            "old_rank": old_rank,
            "new_rank": new_rank,
            "ep": ep,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
        })
        _save_raw(data)
