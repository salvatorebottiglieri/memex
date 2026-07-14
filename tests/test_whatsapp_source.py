"""Tests for WhatsApp export parser (InboxSource adapter).

Tests exercise the public interface only:
- `parse_whatsapp_export(text)` yields dicts with url, timestamp, note
- Non-link messages are silently dropped
- The note is the message text with the URL stripped and whitespace-trimmed
"""
from __future__ import annotations

import pytest
from memex.whatsapp_source import parse_whatsapp_export


FIXTURE_EXPORT = """\
[01/06/2024, 09:15:32] Alice: https://example.com/article
[01/06/2024, 10:00:00] Bob: Check this out https://news.example.com/story interesting read
[01/06/2024, 11:30:45] Alice: Just catching up, no links here
[02/06/2024, 08:00:00] Bob: https://blog.example.com/post?utm_source=twitter
[02/06/2024, 09:00:00] Alice: Morning!
"""


class TestParseWhatsAppExport:
    def test_messages_with_urls_are_returned(self):
        items = list(parse_whatsapp_export(FIXTURE_EXPORT))
        assert len(items) == 3

    def test_non_link_messages_are_silently_ignored(self):
        items = list(parse_whatsapp_export(FIXTURE_EXPORT))
        urls = [item["url"] for item in items]
        assert "Just catching up, no links here" not in urls
        assert "Morning!" not in urls

    def test_url_is_extracted_correctly(self):
        items = list(parse_whatsapp_export(FIXTURE_EXPORT))
        assert items[0]["url"] == "https://example.com/article"

    def test_timestamp_is_extracted_as_iso8601(self):
        items = list(parse_whatsapp_export(FIXTURE_EXPORT))
        # [01/06/2024, 09:15:32] -> 2024-06-01T09:15:32
        assert items[0]["timestamp"] == "2024-06-01T09:15:32"

    def test_note_is_adjacent_text_without_url(self):
        items = list(parse_whatsapp_export(FIXTURE_EXPORT))
        # "Check this out https://news.example.com/story interesting read"
        # -> note = "Check this out  interesting read" (stripped)
        assert items[1]["note"] == "Check this out interesting read"

    def test_note_is_none_when_message_is_only_url(self):
        items = list(parse_whatsapp_export(FIXTURE_EXPORT))
        assert items[0].get("note") is None

    def test_note_key_absent_when_no_adjacent_text(self):
        """Items with URL-only messages should not include 'note' key."""
        items = list(parse_whatsapp_export(FIXTURE_EXPORT))
        assert "note" not in items[0]

    def test_url_preserved_verbatim_including_tracking_params(self):
        """Parser preserves raw URL; canonicalisation is the ingest layer's job."""
        items = list(parse_whatsapp_export(FIXTURE_EXPORT))
        assert items[2]["url"] == "https://blog.example.com/post?utm_source=twitter"

    def test_empty_export_yields_nothing(self):
        items = list(parse_whatsapp_export(""))
        assert items == []

    def test_multiline_system_messages_ignored(self):
        """WhatsApp system messages like '‎Messages to this group are now secured' are ignored."""
        text = """\
[01/06/2024, 09:00:00] ‎Messages to this group are now secured with end-to-end encryption.
[01/06/2024, 09:05:00] Alice: https://example.com/real
"""
        items = list(parse_whatsapp_export(text))
        assert len(items) == 1
        assert items[0]["url"] == "https://example.com/real"
