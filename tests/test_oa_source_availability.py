"""Get PDF searches three sources; one of them needs configuring.

Unpaywall requires a contact address — `metrics.fetch_oa_locations`
returns None without one. Silently consulting two sources instead of
three looks exactly like finding nothing, so the user never learns
that a preference would have helped. `unavailable_sources()` names
what is being skipped so the caller can say so.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from alexandria import pdf_fetch


def test_unpaywall_is_named_when_no_contact_email(monkeypatch):
    monkeypatch.setattr(pdf_fetch.metrics, "OPENALEX_MAILTO", "")
    assert pdf_fetch.unavailable_sources() == ["Unpaywall"]


def test_nothing_is_skipped_once_an_address_is_set(monkeypatch):
    monkeypatch.setattr(pdf_fetch.metrics, "OPENALEX_MAILTO",
                        "someone@example.org")
    assert pdf_fetch.unavailable_sources() == []


def test_the_chain_still_returns_the_other_sources(monkeypatch):
    """A missing address costs Unpaywall, not the whole feature."""
    monkeypatch.setattr(pdf_fetch.metrics, "OPENALEX_MAILTO", "")
    monkeypatch.setattr(pdf_fetch, "_openalex_pdf_urls",
                        lambda d: ["https://oa.example/one.pdf"])
    monkeypatch.setattr(pdf_fetch, "_europepmc_pdf_urls",
                        lambda d, timeout=15: ["https://pmc.example/two.pdf"])

    urls = pdf_fetch.oa_pdf_urls_for_doi("10.1/x")

    assert urls == ["https://oa.example/one.pdf",
                    "https://pmc.example/two.pdf"]


def test_source_order_and_dedup(monkeypatch):
    """OpenAlex first, EuropePMC last, each URL once — the order the
    GUI now inherits instead of hand-rolling its own."""
    monkeypatch.setattr(pdf_fetch.metrics, "OPENALEX_MAILTO",
                        "someone@example.org")
    shared = "https://same.example/paper.pdf"
    monkeypatch.setattr(pdf_fetch, "_openalex_pdf_urls",
                        lambda d: ["https://oa.example/first.pdf", shared])
    monkeypatch.setattr(pdf_fetch, "_unpaywall_pdf_urls",
                        lambda d: [shared, "https://unpaywall.example/u.pdf"])
    monkeypatch.setattr(pdf_fetch, "_europepmc_pdf_urls",
                        lambda d, timeout=15: [shared,
                                               "https://pmc.example/e.pdf"])

    assert pdf_fetch.oa_pdf_urls_for_doi("10.1/x") == [
        "https://oa.example/first.pdf",
        shared,
        "https://unpaywall.example/u.pdf",
        "https://pmc.example/e.pdf",
    ]


def test_europepmc_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(pdf_fetch.metrics, "OPENALEX_MAILTO",
                        "someone@example.org")
    monkeypatch.setattr(pdf_fetch, "_openalex_pdf_urls", lambda d: [])
    monkeypatch.setattr(pdf_fetch, "_unpaywall_pdf_urls", lambda d: [])
    called = []
    monkeypatch.setattr(pdf_fetch, "_europepmc_pdf_urls",
                        lambda d, timeout=15: called.append(d) or ["x"])

    assert pdf_fetch.oa_pdf_urls_for_doi("10.1/x",
                                         also_try_europepmc=False) == []
    assert called == []
