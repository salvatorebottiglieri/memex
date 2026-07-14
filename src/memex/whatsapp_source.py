"""WhatsApp export parser — InboxSource adapter.

Parses a WhatsApp `.txt` chat export and yields captured items:
  { url, timestamp, note? }

WhatsApp message formats:
  [DD/MM/YYYY, HH:MM:SS] Author: message text       (export with seconds)
  DD/MM/YY, HH:MM - Author: message text             (export without seconds)

Only messages containing a URL are emitted. Non-link chatter is silently ignored.
The timestamp is taken from the message header and returned as ISO 8601.
Any text adjacent to the URL (with URL stripped) becomes the note.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator, TypedDict


# Matches WhatsApp message headers in two common export formats:
_HEADER_RE = re.compile(
    r"^\[?"
    r"(\d{2}/\d{2}/\d{2,4}),\s*"
    r"(\d{2}:\d{2}(?::\d{2})?)"
    r"\]?\s*(?:-\s*)?"
    r"([^:]+):\s*"
)

# Matches a URL in text (greedy, stops at whitespace)
_URL_RE = re.compile(r"https?://\S+")


class CapturedItem(TypedDict, total=False):
    url: str
    timestamp: str
    note: str


def parse_whatsapp_export(text: str) -> Iterator[CapturedItem]:
    """Parse a WhatsApp `.txt` export and yield captured items.

    Each item is a dict with:
      - url (str): the raw URL as it appeared in the message
      - timestamp (str): ISO 8601 datetime from the message header
      - note (str, optional): adjacent non-URL text, present only when non-empty
    """
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if not m:
            # System message or continuation line — skip
            continue

        date_str, time_str, _author = m.group(1), m.group(2), m.group(3)
        message_text = line[m.end():]

        url_match = _URL_RE.search(message_text)
        if not url_match:
            continue

        # Parse timestamp -> ISO 8601 (handle 2 or 4 digit year, optional seconds)
        raw_date = date_str
        if len(date_str.split("/")[-1]) == 2:
            # Two-digit year: assume 20xx
            parts = date_str.split("/")
            raw_date = f"{parts[0]}/{parts[1]}/20{parts[2]}"
        fmt = "%d/%m/%Y %H:%M:%S" if time_str.count(":") == 2 else "%d/%m/%Y %H:%M"
        dt = datetime.strptime(f"{raw_date} {time_str}", fmt)
        timestamp = dt.isoformat()

        url = url_match.group(0)

        # Build note: message text with URL removed, whitespace collapsed
        note_text = _URL_RE.sub("", message_text).strip()
        note_text = re.sub(r"  +", " ", note_text)

        item: CapturedItem = {"url": url, "timestamp": timestamp}
        if note_text:
            item["note"] = note_text

        yield item
