from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SourceDocument:
    url: str
    title: str = ""
    text: str = ""
    publisher: str = ""
    og_image: str | None = None
    links: list[str] = field(default_factory=list)
    error: str | None = None
    linkable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceDocument:
        return cls(**value)


@dataclass
class StoryCandidate:
    origin_type: str
    origin_ref: str
    title: str
    text: str
    urls: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    telegram_file_id: str | None = None
    telegram_media_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StoryCandidate:
        return cls(**value)


@dataclass
class TriageDecision:
    relevant: bool
    score: float
    category: str
    reason: str
    industry_critical: bool = False
    primary_source_index: int = 0
    linkable_source_indices: list[int] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TriageDecision:
        return cls(**value)


@dataclass
class GeneratedPost:
    title: str
    body: str
    hashtags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GeneratedPost:
        return cls(**value)
