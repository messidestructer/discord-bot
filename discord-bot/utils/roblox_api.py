"""
Roblox API helper functions.
Uses the free Roblox public API — no paid services.
"""
import asyncio
import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger("roblox_api")

ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "")
GROUP_ID = os.getenv("ROBLOX_GROUP_ID", "")

_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        headers = {}
        if ROBLOX_COOKIE:
            headers["Cookie"] = f".ROBLOSECURITY={ROBLOX_COOKIE}"
        _session = aiohttp.ClientSession(headers=headers)
    return _session


# ── User lookup ──────────────────────────────────────────────────────────────

async def get_user_id_by_name(username: str) -> Optional[int]:
    session = await get_session()
    try:
        async with session.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("data"):
                return data["data"][0]["id"]
    except Exception as e:
        log.error(f"get_user_id_by_name({username}): {e}")
    return None


async def get_user_profile(user_id: int) -> Optional[dict]:
    session = await get_session()
    try:
        async with session.get(f"https://users.roblox.com/v1/users/{user_id}") as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        log.error(f"get_user_profile({user_id}): {e}")
    return None


async def get_profile_description(user_id: int) -> Optional[str]:
    profile = await get_user_profile(user_id)
    if profile:
        return profile.get("description", "")
    return None


# ── Group helpers ────────────────────────────────────────────────────────────

async def get_group_rank(user_id: int, group_id: str = None) -> Optional[int]:
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.get(
            f"https://groups.roblox.com/v2/users/{user_id}/groups/roles"
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            for entry in data.get("data", []):
                if str(entry["group"]["id"]) == str(gid):
                    return entry["role"]["rank"]
    except Exception as e:
        log.error(f"get_group_rank({user_id}): {e}")
    return None


async def get_group_role_name(user_id: int, group_id: str = None) -> Optional[str]:
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.get(
            f"https://groups.roblox.com/v2/users/{user_id}/groups/roles"
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            for entry in data.get("data", []):
                if str(entry["group"]["id"]) == str(gid):
                    return entry["role"]["name"]
    except Exception as e:
        log.error(f"get_group_role_name({user_id}): {e}")
    return None


async def get_group_roles(group_id: str = None) -> list:
    """Returns list of {id, name, rank, memberCount} for all roles in the group."""
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.get(f"https://groups.roblox.com/v1/groups/{gid}/roles") as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("roles", [])
    except Exception as e:
        log.error(f"get_group_roles(): {e}")
    return []


async def set_group_rank(user_id: int, rank_id: int, group_id: str = None) -> bool:
    """Set a user's rank in the group by role ID (not rank number)."""
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.patch(
            f"https://groups.roblox.com/v1/groups/{gid}/users/{user_id}",
            json={"roleId": rank_id},
        ) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"set_group_rank({user_id}, {rank_id}): {e}")
    return False


async def set_group_rank_by_number(user_id: int, rank_number: int, group_id: str = None) -> bool:
    """Set rank by rank number (1-255). Looks up the matching role ID first."""
    roles = await get_group_roles(group_id)
    for role in roles:
        if role["rank"] == rank_number:
            return await set_group_rank(user_id, role["id"], group_id)
    log.warning(f"Rank number {rank_number} not found in group roles.")
    return False


async def kick_from_group(user_id: int, group_id: str = None) -> bool:
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.delete(
            f"https://groups.roblox.com/v1/groups/{gid}/users/{user_id}"
        ) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"kick_from_group({user_id}): {e}")
    return False


# ── Join requests ────────────────────────────────────────────────────────────

async def get_join_requests(group_id: str = None, limit: int = 20) -> list:
    gid = group_id or GROUP_ID
    session = await get_session()
    requests = []
    cursor = ""
    while True:
        try:
            url = f"https://groups.roblox.com/v1/groups/{gid}/join-requests?limit={limit}&sortOrder=Asc"
            if cursor:
                url += f"&cursor={cursor}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
                requests.extend(data.get("data", []))
                cursor = data.get("nextPageCursor", "")
                if not cursor or len(requests) >= 100:
                    break
        except Exception as e:
            log.error(f"get_join_requests(): {e}")
            break
    return requests


async def accept_join_request(user_id: int, group_id: str = None) -> bool:
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.post(
            f"https://groups.roblox.com/v1/groups/{gid}/join-requests/users/{user_id}"
        ) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"accept_join_request({user_id}): {e}")
    return False


async def deny_join_request(user_id: int, group_id: str = None) -> bool:
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.delete(
            f"https://groups.roblox.com/v1/groups/{gid}/join-requests/users/{user_id}"
        ) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"deny_join_request({user_id}): {e}")
    return False


# ── Bloxlink integration ─────────────────────────────────────────────────────

BLOXLINK_KEY = os.getenv("BLOXLINK_API_KEY", "")


async def bloxlink_get_roblox_id(discord_id: int, guild_id: str) -> Optional[int]:
    """Look up a Discord user's Roblox ID via Bloxlink (free tier)."""
    if not BLOXLINK_KEY:
        return None
    session = await get_session()
    try:
        async with session.get(
            f"https://api.blox.link/v4/public/guilds/{guild_id}/discord-to-roblox/{discord_id}",
            headers={"Authorization": BLOXLINK_KEY},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                roblox_id = data.get("robloxID")
                if roblox_id:
                    return int(roblox_id)
    except Exception as e:
        log.error(f"bloxlink_get_roblox_id({discord_id}): {e}")
    return None
