"""Return-type dataclasses for the tool surface.

FastMCP auto-generates JSON schemas from these annotations, so the
LLM sees structured shapes rather than bare dicts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PaperSummary:
    """Minimal record for search / find results — just enough for
    the LLM (or user) to disambiguate which paper is which."""
    id: int
    title: Optional[str]
    first_author: Optional[str]
    last_author: Optional[str]
    year: Optional[int]
    journal: Optional[str]
    doi: Optional[str]
    citations: Optional[int]
    is_ghost: bool


@dataclass
class PdfText:
    """One paper's extracted PDF text, sliced to a page range and
    char cap. `error` is set instead of `text` for ghosts
    (no PDF on disk), missing files, or extractor failures."""
    paper_id: int
    page_count: Optional[int]
    page_from: int
    page_to: Optional[int]
    text: str
    truncated: bool
    error: Optional[str]


@dataclass
class PaperDetail:
    """Full paper record from the indexed columns. Returned by
    `get_papers`. For fields the database doesn't promote to
    columns (raw OpenAlex payloads, hand-edited custom fields,
    comments), use `get_sidecars` instead."""
    # PaperSummary fields, repeated so this is a flat dataclass
    # rather than an inheritance chain (FastMCP's schema generator
    # handles flat dataclasses more reliably).
    id: int
    title: Optional[str]
    first_author: Optional[str]
    last_author: Optional[str]
    year: Optional[int]
    journal: Optional[str]
    doi: Optional[str]
    citations: Optional[int]
    is_ghost: bool
    # Extended fields.
    authors: list[str] = field(default_factory=list)
    abstract: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    mark: Optional[str] = None
    is_oa: Optional[bool] = None
    oa_status: Optional[str] = None
    license_label: Optional[str] = None
    added_date: Optional[str] = None
    pdf_path: Optional[str] = None
    sidecar_path: Optional[str] = None
    citations_by_year: list[dict[str, Any]] = field(default_factory=list)
    funders: list[str] = field(default_factory=list)
    grants: list[dict[str, Any]] = field(default_factory=list)
