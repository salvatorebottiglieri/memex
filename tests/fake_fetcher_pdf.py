"""Fake PDF fetcher for smoke tests — returns PDF-shaped content."""

from memex.fetcher import FetchResult


class FakePDFFetcher:
    """Deterministic fetcher that mimics PDF-extracted text."""

    def fetch(self, url: str):
        return FetchResult(
            content=(
                "# Extracted PDF Research Paper\n\n"
                "This is the extracted text content from a PDF file. "
                "It contains plain text without any HTML markup, mimicking "
                "what pypdf would extract from a real PDF document. "
                "This text is intentionally longer than one hundred characters "
                "so that the L0 markdown file is created during ingestion."
            ),
        )
