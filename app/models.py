"""轻量数据对象（DB 行的内存表示）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Version:
    procedure_no: str
    version_no: int
    valid_start: str
    valid_end: str
    body: str
    published_at: str
    supersedes_version: Optional[int] = None

    @classmethod
    def from_row(cls, row) -> "Version":
        return cls(
            procedure_no=row["procedure_no"],
            version_no=row["version_no"],
            valid_start=row["valid_start"],
            valid_end=row["valid_end"],
            body=row["body"],
            published_at=row["published_at"],
            supersedes_version=row["supersedes_version"],
        )

    def to_json(self) -> dict:
        return {
            "procedure_no": self.procedure_no,
            "version": self.version_no,
            "valid_start": self.valid_start,
            "valid_end": self.valid_end,
            "body": self.body,
            "published_at": self.published_at,
            "supersedes_version": self.supersedes_version,
        }
