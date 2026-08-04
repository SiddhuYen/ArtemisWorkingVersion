"""Persistence for the wrapper.

Boards only. The relationship-graph tables that used to live here — people,
organizations, sources, edges, local profiles, matches, candidate paths,
enrichment runs — belonged to the deleted engine. Routes are researched live and
verified per run; nothing about them is accumulated into a local graph, so there
is nothing else to store.

A board is where a found route gets displayed and kept.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.types import JSON

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


BOARD_STATUSES = ("active", "archived")


class Board(Base):
    __tablename__ = "boards"

    id = Column(String, primary_key=True, default=_uuid)
    owner_id = Column(String, index=True, nullable=False)
    name = Column(String, nullable=False)
    target_name = Column(String, nullable=True)
    target_org = Column(String, nullable=True)
    status = Column(String, default="active")  # one of BOARD_STATUSES
    created_at = Column(String, default=lambda: _now().isoformat())


class BoardPage(Base):
    __tablename__ = "board_pages"

    id = Column(String, primary_key=True, default=_uuid)
    board_id = Column(String, ForeignKey("boards.id"), nullable=False, index=True)
    name = Column(String, default="Page 1")
    position = Column(Integer, default=0)   # tab ordering
    elements = Column(JSON, default=dict)   # {"nodes": [...], "edges": [...], "centerId": ...}
    created_at = Column(String, default=lambda: _now().isoformat())


class Contact(Base):
    """One person from the operator's own imported network.

    `owner_norm` is what makes a row *someone's* connection rather than a fact
    about the world: these are only usable as first-degree ties for the person
    who uploaded them, so every lookup is scoped by it. Without that scope a
    shared deployment would let one operator route through another's contacts.
    """

    __tablename__ = "contacts"

    id = Column(String, primary_key=True, default=_uuid)
    owner_norm = Column(String, index=True, nullable=False)
    canonical_name = Column(String, nullable=False)
    norm_name = Column(String, index=True, nullable=False)  # for dedupe on re-import
    company = Column(String, nullable=True)
    title = Column(String, nullable=True)
    email = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    connected_on = Column(String, nullable=True)
    source = Column(String, default="linkedin_csv")  # or "vcard"
    created_at = Column(String, default=lambda: _now().isoformat())
