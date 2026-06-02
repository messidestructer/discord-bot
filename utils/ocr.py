"""
OCR helpers using pytesseract (Tesseract-OCR — completely free).
Extracts Roblox usernames from event screenshots.

Improvements over v1:
- Better image pre-processing (greyscale, contrast boost, upscale)
- Smarter username extraction (handles common OCR misreads)
- Graceful degradation if Tesseract/Pillow not installed
"""
import io
import logging
import re
from typing import Optional

log = logging.getLogger("ocr")

# Roblox usernames: 3-20 chars, alphanumeric + underscore only
_USERNAME_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{2,19})\b")

# Common words that appear in Roblox UIs — not usernames
_IGNORE_WORDS = frozenset({
    "roblox", "game", "players", "server", "online", "team", "group",
    "report", "leave", "join", "chat", "party", "friends", "followers",
    "event", "host", "rank", "role", "staff", "admin", "mod", "vip",
    "the", "and", "for", "you", "are", "was", "has", "have", "with",
    "from", "that", "this", "can", "all", "not", "but", "had", "its",
    "also", "via", "our", "their", "your", "him", "her", "they", "we",
    "score", "total", "points", "time", "date", "info", "list", "menu",
    "click", "button", "open", "close", "send", "type", "name", "user",
    "true", "false", "null", "none", "yes", "no", "ok", "cancel",
})


def _preprocess_image(image_bytes: bytes):
    """
    Load and pre-process the image for best OCR accuracy.
    Returns a PIL Image, or None on failure.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter
    except ImportError:
        return None

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # Upscale small images — Tesseract performs better at ~300 DPI
        w, h = img.size
        if w < 1200:
            scale = 1200 / w
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        # Convert to greyscale
        img = img.convert("L")

        # Boost contrast
        img = ImageEnhance.Contrast(img).enhance(2.0)

        # Sharpen
        img = img.filter(ImageFilter.SHARPEN)

        return img
    except Exception as e:
        log.warning(f"Image preprocessing failed: {e}")
        return None


def extract_usernames_from_image(image_bytes: bytes) -> list[str]:
    """
    Run Tesseract OCR on image_bytes and return a deduplicated list of
    candidate Roblox usernames, best-quality first.

    Returns [] if pytesseract / Tesseract is not installed, or on any error.
    """
    try:
        import pytesseract
    except ImportError:
        log.warning("pytesseract not installed — OCR unavailable. Run: pip install pytesseract")
        return []

    img = _preprocess_image(image_bytes)
    if img is None:
        return []

    try:
        # PSM 4: assume a single column of text (good for player lists)
        # PSM 6 fallback for more complex layouts
        results: list[str] = []
        for psm in (4, 6):
            config = (
                f"--psm {psm} "
                "-c tessedit_char_whitelist="
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_ "
            )
            raw = pytesseract.image_to_string(img, config=config)
            log.debug(f"OCR PSM {psm} raw (first 300 chars): {raw[:300]!r}")
            parsed = _parse_usernames(raw)
            # Merge, preserving first-seen order
            existing = {u.lower() for u in results}
            for u in parsed:
                if u.lower() not in existing:
                    results.append(u)
                    existing.add(u.lower())
        return results
    except Exception as e:
        log.error(f"OCR failed: {e}")
        return []


def _parse_usernames(text: str) -> list[str]:
    """Extract and deduplicate valid-looking usernames from raw OCR text."""
    seen:   set[str]  = set()
    result: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for token in _USERNAME_RE.findall(line):
            low = token.lower()
            if low in _IGNORE_WORDS:
                continue
            if len(token) < 3:
                continue
            # Skip tokens that are all digits
            if token.isdigit():
                continue
            if low not in seen:
                seen.add(low)
                result.append(token)

    return result


def parse_manual_names(text: str) -> list[str]:
    """Parse a newline-separated list of Roblox usernames entered by staff."""
    seen:   set[str]  = set()
    result: list[str] = []

    for line in text.splitlines():
        name = line.strip()
        if not name:
            continue
        # Roblox username rules: 3-20 chars, alphanumeric + underscores
        if not re.match(r"^[A-Za-z0-9_]{3,20}$", name):
            continue
        low = name.lower()
        if low not in seen:
            seen.add(low)
            result.append(name)

    return result