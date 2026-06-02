"""
OCR helpers using pytesseract (Tesseract-OCR — completely free).
Extracts Roblox usernames from event screenshots.
No paid AI services.
"""
import io
import logging
import re
from typing import Optional

log = logging.getLogger("ocr")

# Roblox usernames: 3-20 chars, letters/numbers/underscores only
_USERNAME_RE = re.compile(r"\b([A-Za-z0-9_]{3,20})\b")

# Words to ignore that commonly appear in event screenshots
_IGNORE_WORDS = {
    "roblox", "game", "players", "server", "online", "team", "group",
    "report", "leave", "join", "chat", "party", "friends", "followers",
    "event", "host", "rank", "role", "staff", "admin", "mod", "vip",
    "the", "and", "for", "you", "are", "was", "has", "have", "with",
    "from", "that", "this", "can", "all", "not", "but", "had",
}


def extract_usernames_from_image(image_bytes: bytes) -> list[str]:
    """
    Run Tesseract OCR on the image and return a list of candidate Roblox usernames.
    Returns empty list if pytesseract is not installed.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        log.warning("pytesseract or Pillow not installed — OCR unavailable")
        return []

    try:
        image = Image.open(io.BytesIO(image_bytes))
        # Upscale if small — helps Tesseract accuracy
        w, h = image.size
        if w < 800:
            scale = 800 / w
            image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        # Tesseract config: PSM 6 = assume uniform block of text
        config = "--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_ "
        raw_text = pytesseract.image_to_string(image, config=config)
        log.debug(f"OCR raw text: {raw_text[:300]}")
        return _parse_usernames(raw_text)
    except Exception as e:
        log.error(f"OCR failed: {e}")
        return []


def _parse_usernames(text: str) -> list[str]:
    seen = set()
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        tokens = _USERNAME_RE.findall(line)
        for token in tokens:
            low = token.lower()
            if low in _IGNORE_WORDS:
                continue
            if len(token) < 3:
                continue
            if low not in seen:
                seen.add(low)
                result.append(token)
    return result


def parse_manual_names(text: str) -> list[str]:
    """Parse a newline-separated list of Roblox usernames typed by staff."""
    seen = set()
    result = []
    for line in text.splitlines():
        name = line.strip()
        if not name:
            continue
        if not re.match(r"^[A-Za-z0-9_]{3,20}$", name):
            continue
        low = name.lower()
        if low not in seen:
            seen.add(low)
            result.append(name)
    return result
