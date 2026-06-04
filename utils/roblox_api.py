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
GROUP_ID      = os.getenv("ROBLOX_GROUP_ID", "")

_session: Optional[aiohttp.ClientSession] = None
_timeout = aiohttp.ClientTimeout(total=15)


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        headers = {}
        if ROBLOX_COOKIE:
            headers["Cookie"] = f".ROBLOSECURITY={ROBLOX_COOKIE}"
        _session = aiohttp.ClientSession(headers=headers, timeout=_timeout)
    return _session


# ── User lookup ───────────────────────────────────────────────────────────────

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


# ── Group helpers ─────────────────────────────────────────────────────────────

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
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.get(f"https://groups.roblox.com/v1/groups/{gid}/roles") as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("roles", [])
            else:
                text = await resp.text()
                log.error(f"get_group_roles: HTTP {resp.status}: {text[:200]}")
    except Exception as e:
        log.error(f"get_group_roles(): {e}")
    return []


async def set_group_rank(user_id: int, rank_id: int, group_id: str = None) -> bool:
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.patch(
            f"https://groups.roblox.com/v1/groups/{gid}/users/{user_id}",
            json={"roleId": rank_id},
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error(f"set_group_rank: HTTP {resp.status}: {text[:200]}")
            return resp.status == 200
    except Exception as e:
        log.error(f"set_group_rank({user_id}, {rank_id}): {e}")
    return False


async def set_group_rank_by_number(user_id: int, rank_number: int, group_id: str = None) -> bool:
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
            if resp.status not in (200, 204):
                text = await resp.text()
                log.error(f"kick_from_group: HTTP {resp.status}: {text[:200]}")
            return resp.status in (200, 204)
    except Exception as e:
        log.error(f"kick_from_group({user_id}): {e}")
    return False


# ── Join requests ─────────────────────────────────────────────────────────────
# The Roblox Groups API for join requests:
#   GET  /v1/groups/{groupId}/join-requests
# Response format:
#   { data: [ { requester: { userId, username, displayName }, created: "..." }, ... ],
#     nextPageCursor: "..." }

async def get_join_requests(group_id: str = None) -> list:
    gid = group_id or GROUP_ID
    if not gid:
        log.error("get_join_requests: ROBLOX_GROUP_ID not set")
        return []

    session  = await get_session()
    requests = []
    cursor   = None

    while True:
        url = f"https://groups.roblox.com/v1/groups/{gid}/join-requests?limit=100&sortOrder=Asc"
        if cursor:
            url += f"&cursor={cursor}"

        try:
            async with session.get(url) as resp:
                text = await resp.text()
                if resp.status == 403:
                    log.error(
                        "get_join_requests: 403 Forbidden — make sure your ROBLOX_COOKIE is valid "
                        "and the bot account can manage join requests."
                    )
                    break
                if resp.status != 200:
                    log.error(f"get_join_requests: HTTP {resp.status}: {text[:300]}")
                    break

                import json
                try:
                    data = json.loads(text)
                except Exception:
                    log.error(f"get_join_requests: invalid JSON: {text[:300]}")
                    break

                page = data.get("data", [])
                log.debug(f"get_join_requests: page of {len(page)} items")
                requests.extend(page)

                cursor = data.get("nextPageCursor")
                if not cursor or len(requests) >= 200:
                    break

        except Exception as e:
            log.error(f"get_join_requests(): {e}")
            break

    log.info(f"get_join_requests: found {len(requests)} pending requests")
    return requests


def _extract_requester(req: dict) -> tuple:
    """
    Normalise a join-request entry into (user_id, username).
    Handles both the 'requester' sub-object format and flat format.
    """
    requester = req.get("requester") or {}
    user_id   = requester.get("userId") or req.get("userId")
    username  = (
        requester.get("username")
        or requester.get("displayName")
        or req.get("username")
        or str(user_id or "?")
    )
    return user_id, username


async def accept_join_request(user_id: int, group_id: str = None) -> bool:
    gid = group_id or GROUP_ID
    session = await get_session()
    try:
        async with session.post(
            f"https://groups.roblox.com/v1/groups/{gid}/join-requests/users/{user_id}"
        ) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                log.error(f"accept_join_request({user_id}): HTTP {resp.status}: {text[:200]}")
            return resp.status in (200, 204)
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
            if resp.status not in (200, 204):
                text = await resp.text()
                log.error(f"deny_join_request({user_id}): HTTP {resp.status}: {text[:200]}")
            return resp.status in (200, 204)
    except Exception as e:
        log.error(f"deny_join_request({user_id}): {e}")
    return False


# ── Bloxlink ──────────────────────────────────────────────────────────────────

async def bloxlink_get_roblox_id(discord_id: int, guild_id: str) -> Optional[int]:
    key = os.getenv("BLOXLINK_API_KEY", "")
    if not key:
        return None
    session = await get_session()
    try:
        # Bloxlink v4 API
        async with session.get(
            f"https://api.blox.link/v4/public/guilds/{guild_id}/discord-to-roblox/{discord_id}",
            headers={"Authorization": key},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                # v4 response: { "robloxID": "12345678" }  or  { "resolved": { "roblox": { "id": ... } } }
                roblox_id = data.get("robloxID") or data.get("robloxId")
                if not roblox_id:
                    # Try alternate field path
                    resolved = data.get("resolved", {})
                    roblox_id = resolved.get("roblox", {}).get("id")
                if roblox_id:
                    return int(roblox_id)
            elif resp.status == 404:
                pass  # User not in Bloxlink
            else:
                text = await resp.text()
                log.warning(f"bloxlink_get_roblox_id({discord_id}): HTTP {resp.status}: {text[:200]}")
    except Exception as e:
        log.error(f"bloxlink_get_roblox_id({discord_id}): {e}")
    return None


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()