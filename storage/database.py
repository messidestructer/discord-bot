"""
JSON-based database for the bot.
Primary keys:
  - EP records:    roblox_id (int, str in JSON)
  - Verified users: discord_id (str) -> {roblox_id, roblox_username}
  - Reverse index:  roblox_id (str) -> discord_id (str)

Using roblox_id as the primary key means username changes are handled
automatically — we just update the stored username on next lookup.
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
    # roblox_id (str) -> {roblox_id, username, ep, discord_id, joined_at, last_updated}
    "ep_records":            {},
    "event_log":             [],   # list of event entry dicts
    "ep_audit_log":          [],   # every EP change ever
    # discord_id (str) -> {roblox_id (int), roblox_username (str)}
    "verified_users":        {},
    # roblox_id (str) -> discord_id (str)  [reverse index]
    "roblox_to_discord":     {},
    "pending_verifications": {},   # discord_id (str) -> {code, roblox_id, roblox_username, expires_at}
    "promotion_log":         [],
    "rank_log":              [],
    "warnings":              {},   # discord_id (str) -> list of warning dicts
    "role_binds":            {},   # roblox_rank (str) -> discord_role_id (int)
}


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _load_raw() -> dict:
    if not os.path.exists(DB_PATH):
        return {k: (list() if isinstance(v, list) else {}) for k, v in _DEFAULT.items()}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        log.exception("Database corrupted, rebuilding.")
        data = {}
    for k, v in _DEFAULT.items():
        if k not in data:
            data[k] = list() if isinstance(v, list) else {}

    # ── One-time migration: old schema used username as key ───────────────
    _migrate_if_needed(data)
    return data


def _migrate_if_needed(data: dict):
    """
    Migrate old verified_users format:
      OLD: discord_id -> roblox_username (str)
      NEW: discord_id -> {roblox_id, roblox_username}
    We can't fetch roblox_ids here (async), so we mark them for later resolution.
    The roblox_to_discord index is rebuilt from verified_users.
    Old ep_records keyed by username are left in place; they will be linked by
    roblox_id once the user is seen again.
    """
    changed = False
    for did, val in list(data["verified_users"].items()):
        if isinstance(val, str):
            # Old format — store username, mark roblox_id as unknown (0)
            data["verified_users"][did] = {
                "roblox_id":       0,    # will be resolved on next verify/use
                "roblox_username": val,
            }
            changed = True

    # Rebuild reverse index
    if "roblox_to_discord" not in data:
        data["roblox_to_discord"] = {}
        changed = True
    for did, val in data["verified_users"].items():
        if isinstance(val, dict) and val.get("roblox_id"):
            rid = str(val["roblox_id"])
            if rid != "0":
                data["roblox_to_discord"][rid] = did

    if changed:
        log.info("Database migrated to roblox_id-keyed schema.")


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

def _ep_key(roblox_id: int) -> str:
    return str(roblox_id)


async def get_ep_by_id(roblox_id: int) -> Optional[dict]:
    data = await load()
    return data["ep_records"].get(_ep_key(roblox_id))


async def get_ep(roblox_username: str) -> Optional[dict]:
    """Convenience: look up EP by username (searches all records)."""
    data = await load()
    low = roblox_username.lower()
    for rec in data["ep_records"].values():
        if rec.get("username", "").lower() == low:
            return rec
    return None


async def get_ep_for_member(discord_id: int) -> Optional[dict]:
    """Get EP record for a Discord member via their linked roblox_id."""
    data = await load()
    val = data["verified_users"].get(str(discord_id))
    if not val or not isinstance(val, dict):
        return None
    rid = val.get("roblox_id", 0)
    if not rid or rid == 0:
        # Fall back to username search
        uname = val.get("roblox_username", "")
        if uname:
            return await get_ep(uname)
        return None
    return data["ep_records"].get(_ep_key(rid))


def _ensure_ep_record(data: dict, roblox_id: int, roblox_username: str, discord_id: Optional[int] = None) -> dict:
    key = _ep_key(roblox_id)
    if key not in data["ep_records"]:
        data["ep_records"][key] = {
            "roblox_id":    roblox_id,
            "username":     roblox_username,
            "ep":           0,
            "discord_id":   discord_id,
            "joined_at":    datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    else:
        # Always keep username current (handles name changes)
        rec = data["ep_records"][key]
        rec["username"] = roblox_username
        if discord_id:
            rec["discord_id"] = discord_id
    return data["ep_records"][key]


async def set_ep(
    roblox_username: str,
    ep_delta:        int,
    editor_discord_id: int,
    note:            str = "",
    roblox_id:       Optional[int] = None,
) -> dict:
    """Add ep_delta to a user's EP. Pass roblox_id when available for accuracy."""
    async with _lock:
        data = _load_raw()

        # Resolve roblox_id if not provided
        if not roblox_id:
            roblox_id = _resolve_roblox_id(data, roblox_username)

        if not roblox_id:
            # Last resort: use username as numeric stand-in (negative hash)
            # This should rarely happen after full migration
            log.warning(f"set_ep: no roblox_id for {roblox_username}, using username key")
            roblox_id = abs(hash(roblox_username.lower())) % (10 ** 9)

        rec = _ensure_ep_record(data, roblox_id, roblox_username, editor_discord_id)
        old_ep    = rec["ep"]
        rec["ep"] = max(0, old_ep + ep_delta)
        rec["last_updated"] = datetime.now(timezone.utc).isoformat()

        data["ep_audit_log"].append({
            "roblox_id":         roblox_id,
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
        return dict(rec)


async def set_ep_by_id(
    roblox_id:         int,
    roblox_username:   str,
    ep_delta:          int,
    editor_discord_id: int,
    note:              str = "",
) -> dict:
    return await set_ep(roblox_username, ep_delta, editor_discord_id, note, roblox_id=roblox_id)


async def set_ep_absolute(
    roblox_username:   str,
    ep_amount:         int,
    editor_discord_id: int,
    note:              str = "",
    roblox_id:         Optional[int] = None,
) -> dict:
    async with _lock:
        data = _load_raw()
        if not roblox_id:
            roblox_id = _resolve_roblox_id(data, roblox_username)
        if not roblox_id:
            roblox_id = abs(hash(roblox_username.lower())) % (10 ** 9)

        rec    = _ensure_ep_record(data, roblox_id, roblox_username, editor_discord_id)
        old_ep = rec["ep"]
        rec["ep"] = max(0, ep_amount)
        rec["last_updated"] = datetime.now(timezone.utc).isoformat()

        data["ep_audit_log"].append({
            "roblox_id":         roblox_id,
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


def _resolve_roblox_id(data: dict, roblox_username: str) -> Optional[int]:
    """Find roblox_id by searching verified_users and ep_records."""
    low = roblox_username.lower()
    # Check ep_records first
    for key, rec in data["ep_records"].items():
        if rec.get("username", "").lower() == low and rec.get("roblox_id"):
            return rec["roblox_id"]
    # Check verified_users
    for val in data["verified_users"].values():
        if isinstance(val, dict) and val.get("roblox_username", "").lower() == low:
            rid = val.get("roblox_id", 0)
            if rid and rid != 0:
                return rid
    return None


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
    participants:    list,   # list of {roblox_id, roblox_username}
    host_discord_id: int,
    host_name:       str,
    note:            str = "",
) -> dict:
    async with _lock:
        data = _load_raw()
        # Store both id and username for each participant
        participant_records = []
        for p in participants:
            if isinstance(p, dict):
                participant_records.append(p)
            else:
                # Legacy: plain string username
                participant_records.append({"roblox_id": 0, "roblox_username": p})

        entry = {
            "event_type":        event_type,
            "ep_awarded":        ep_awarded,
            "participants":      participant_records,
            "participant_count": len(participant_records),
            "host_discord_id":   host_discord_id,
            "host_name":         host_name,
            "note":              note,
            "logged_at":         datetime.now(timezone.utc).isoformat(),
        }
        data["event_log"].append(entry)
        _save_raw(data)
        return entry


async def get_recent_events(limit: int = 50) -> list:
    data = await load()
    return list(reversed(data["event_log"][-limit:]))


async def get_events_this_week() -> list:
    from datetime import timedelta
    data = await load()
    now  = datetime.now(timezone.utc)
    days_since_sunday = (now.weekday() + 1) % 7
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

async def is_roblox_verified(roblox_id: int = 0, roblox_username: str = "") -> bool:
    """Check if a roblox account is already linked. Prefer checking by ID."""
    data = await load()
    if roblox_id and roblox_id != 0:
        return str(roblox_id) in data.get("roblox_to_discord", {})
    if roblox_username:
        low = roblox_username.lower()
        for val in data["verified_users"].values():
            if isinstance(val, dict) and val.get("roblox_username", "").lower() == low:
                return True
    return False


async def store_pending_verification(
    discord_id: int, roblox_id: int, roblox_username: str, code: str, expires_at: str
):
    async with _lock:
        data = _load_raw()
        data["pending_verifications"][str(discord_id)] = {
            "roblox_id":       roblox_id,
            "roblox_username": roblox_username,
            "code":            code,
            "expires_at":      expires_at,
        }
        _save_raw(data)


async def get_pending_verification(discord_id: int) -> Optional[dict]:
    data = await load()
    return data["pending_verifications"].get(str(discord_id))


async def get_roblox_info(discord_id: int) -> Optional[dict]:
    """Returns {roblox_id, roblox_username} or None."""
    data = await load()
    val = data["verified_users"].get(str(discord_id))
    if not val:
        return None
    if isinstance(val, dict):
        return val
    # Legacy plain string
    return {"roblox_id": 0, "roblox_username": val}


async def get_roblox_username(discord_id: int) -> Optional[str]:
    """Convenience wrapper — returns just the username."""
    info = await get_roblox_info(discord_id)
    return info["roblox_username"] if info else None


async def get_roblox_id_for_member(discord_id: int) -> Optional[int]:
    """Returns the roblox_id linked to a Discord member, or None."""
    info = await get_roblox_info(discord_id)
    if not info:
        return None
    rid = info.get("roblox_id", 0)
    return rid if rid and rid != 0 else None


async def get_discord_id_for_roblox(roblox_id: int = 0, roblox_username: str = "") -> Optional[int]:
    data = await load()
    if roblox_id and roblox_id != 0:
        did = data.get("roblox_to_discord", {}).get(str(roblox_id))
        if did:
            return int(did)
    if roblox_username:
        low = roblox_username.lower()
        for did, val in data["verified_users"].items():
            if isinstance(val, dict) and val.get("roblox_username", "").lower() == low:
                return int(did)
    return None


async def claim_verification(discord_id: int, roblox_id: int, roblox_username: str) -> bool:
    """
    Link discord_id ↔ roblox_id/username.
    Returns False if roblox_id is already claimed by a different Discord account.
    """
    async with _lock:
        data = _load_raw()

        # Check conflict by roblox_id
        existing_did = data.get("roblox_to_discord", {}).get(str(roblox_id))
        if existing_did and int(existing_did) != discord_id:
            return False

        data["verified_users"][str(discord_id)] = {
            "roblox_id":       roblox_id,
            "roblox_username": roblox_username,
        }
        data.setdefault("roblox_to_discord", {})[str(roblox_id)] = str(discord_id)
        data["pending_verifications"].pop(str(discord_id), None)

        # Link roblox_id to EP record, create if missing
        key = _ep_key(roblox_id)
        if key not in data["ep_records"]:
            data["ep_records"][key] = {
                "roblox_id":    roblox_id,
                "username":     roblox_username,
                "ep":           0,
                "discord_id":   discord_id,
                "joined_at":    datetime.now(timezone.utc).isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        else:
            rec = data["ep_records"][key]
            rec["discord_id"] = discord_id
            rec["username"]   = roblox_username  # update in case of name change

        _save_raw(data)
        return True


async def update_roblox_username(discord_id: int, new_username: str):
    """Call this whenever we detect a username change for a verified member."""
    async with _lock:
        data = _load_raw()
        val = data["verified_users"].get(str(discord_id))
        if val and isinstance(val, dict):
            val["roblox_username"] = new_username
            rid = val.get("roblox_id", 0)
            if rid and rid != 0:
                key = _ep_key(rid)
                if key in data["ep_records"]:
                    data["ep_records"][key]["username"] = new_username
        _save_raw(data)


async def remove_verification(discord_id: int):
    async with _lock:
        data = _load_raw()
        val = data["verified_users"].pop(str(discord_id), None)
        data["pending_verifications"].pop(str(discord_id), None)

        if isinstance(val, dict):
            rid = str(val.get("roblox_id", 0))
            data.get("roblox_to_discord", {}).pop(rid, None)
            if rid in data["ep_records"]:
                data["ep_records"][rid]["discord_id"] = None

        _save_raw(data)


# ── Promotion / rank logs ──────────────────────────────────────────────────────

async def log_promotion(roblox_username: str, old_rank: int, new_rank: int, ep: int):
    async with _lock:
        data = _load_raw()
        data["promotion_log"].append({
            "roblox_username": roblox_username,
            "old_rank":  old_rank,
            "new_rank":  new_rank,
            "ep":        ep,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
        })
        _save_raw(data)


async def log_rank_change(roblox_username: str, old_rank: int, new_rank: int, editor_id: int):
    async with _lock:
        data = _load_raw()
        data["rank_log"].append({
            "roblox_username": roblox_username,
            "old_rank":  old_rank,
            "new_rank":  new_rank,
            "editor_id": editor_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        _save_raw(data)