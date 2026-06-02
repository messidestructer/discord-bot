"""
Google Sheets sync — pushes EP leaderboard and event log to a spreadsheet.
Requires ENABLE_SHEETS=true and a service account credentials file.
"""
import logging
import os
from datetime import datetime, timezone
from typing import List

log = logging.getLogger("sheets")

ENABLE_SHEETS = os.getenv("ENABLE_SHEETS", "false").lower() == "true"
CREDS_FILE = os.getenv("GOOGLE_SHEETS_CREDS_FILE", "credentials.json")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
EVENT_LOG_TAB = os.getenv("GOOGLE_EVENT_LOG_TAB", "Event Log")
EP_TAB = os.getenv("GOOGLE_EP_TAB", "EP Leaderboard")


def _get_service():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as e:
        log.error(f"Google Sheets init failed: {e}")
        return None


def _ensure_tab(service, spreadsheet_id: str, tab_name: str):
    """Create tab if it doesn't exist."""
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if tab_name not in sheets:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
            ).execute()
    except Exception as e:
        log.error(f"_ensure_tab({tab_name}): {e}")


def _write_tab(service, spreadsheet_id: str, tab_name: str, rows: list):
    try:
        range_ = f"{tab_name}!A1"
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A:Z",
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()
    except Exception as e:
        log.error(f"_write_tab({tab_name}): {e}")


async def sync_ep_leaderboard(ep_records: dict):
    if not ENABLE_SHEETS or not SHEET_ID:
        return
    try:
        service = _get_service()
        if not service:
            return
        _ensure_tab(service, SHEET_ID, EP_TAB)
        rows = [["Rank", "Roblox Username", "EP", "Discord ID", "Last Updated"]]
        sorted_records = sorted(ep_records.values(), key=lambda r: r["ep"], reverse=True)
        for i, rec in enumerate(sorted_records, 1):
            rows.append([
                i,
                rec.get("username", ""),
                rec.get("ep", 0),
                rec.get("discord_id", ""),
                rec.get("last_updated", ""),
            ])
        _write_tab(service, SHEET_ID, EP_TAB, rows)
        log.info(f"Synced {len(sorted_records)} EP records to Sheets.")
    except Exception as e:
        log.error(f"sync_ep_leaderboard: {e}")


async def sync_event_log(events: list):
    if not ENABLE_SHEETS or not SHEET_ID:
        return
    try:
        service = _get_service()
        if not service:
            return
        _ensure_tab(service, SHEET_ID, EVENT_LOG_TAB)
        rows = [["Date", "Event Type", "Host", "Attendees", "EP Awarded", "Participants"]]
        for ev in reversed(events):
            rows.append([
                ev.get("logged_at", ""),
                ev.get("event_type", ""),
                ev.get("host_name", ""),
                ev.get("participant_count", 0),
                ev.get("ep_awarded", 0),
                ", ".join(ev.get("participants", [])),
            ])
        _write_tab(service, SHEET_ID, EVENT_LOG_TAB, rows)
        log.info(f"Synced {len(events)} events to Sheets.")
    except Exception as e:
        log.error(f"sync_event_log: {e}")


async def generate_weekly_report_data(events_this_week: list, ep_records: dict) -> dict:
    """Compute weekly stats for the report — no paid AI needed."""
    total_events = len(events_this_week)
    total_ep = sum(e.get("ep_awarded", 0) * e.get("participant_count", 0) for e in events_this_week)

    # EP earned this week per player
    weekly_ep: dict[str, int] = {}
    for ev in events_this_week:
        ep = ev.get("ep_awarded", 0)
        for p in ev.get("participants", []):
            weekly_ep[p] = weekly_ep.get(p, 0) + ep

    # Events hosted per person
    events_hosted: dict[str, int] = {}
    for ev in events_this_week:
        host = ev.get("host_name", "Unknown")
        events_hosted[host] = events_hosted.get(host, 0) + 1

    most_active = max(weekly_ep.items(), key=lambda x: x[1]) if weekly_ep else ("N/A", 0)
    most_hosting = max(events_hosted.items(), key=lambda x: x[1]) if events_hosted else ("N/A", 0)
    unique_members = len(weekly_ep)

    # Top 10 EP overall
    all_sorted = sorted(ep_records.values(), key=lambda r: r["ep"], reverse=True)[:10]

    return {
        "total_events": total_events,
        "total_ep_awarded": total_ep,
        "unique_participants": unique_members,
        "most_active_player": most_active[0],
        "most_active_ep": most_active[1],
        "most_events_hosted": most_hosting[0],
        "most_events_hosted_count": most_hosting[1],
        "top_10_leaderboard": all_sorted,
        "events_by_type": _count_by_type(events_this_week),
        "weekly_ep_gainers": sorted(weekly_ep.items(), key=lambda x: x[1], reverse=True)[:10],
    }


def _count_by_type(events: list) -> dict:
    out = {}
    for ev in events:
        t = ev.get("event_type", "Unknown")
        out[t] = out.get(t, 0) + 1
    return out
