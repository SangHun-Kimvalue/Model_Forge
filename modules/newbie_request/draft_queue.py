from __future__ import annotations

import json
import re
from pathlib import Path

from modules.newbie_request.asset_draft_schemas import (
    DraftAssetEntry,
    DraftAssetEvidenceRecord,
    DraftAssetReviewRecord,
)


class DraftQueueError(ValueError):
    """Raised when draft queue storage receives invalid or inconsistent data."""


class DraftReviewQueue:
    """Repo-local/testable storage seam for draft asset review evidence."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def save_draft(self, draft: DraftAssetEntry) -> Path:
        path = self._draft_path(draft.draft_asset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, draft.model_dump(mode="json"))
        return path

    def load_draft(self, draft_asset_id: str) -> DraftAssetEntry:
        path = self._draft_path(draft_asset_id)
        if not path.is_file():
            raise DraftQueueError(f"Draft asset not found: {draft_asset_id}")
        return DraftAssetEntry.model_validate(_read_json(path))

    def list_drafts(self) -> tuple[DraftAssetEntry, ...]:
        draft_dir = self.root / "drafts"
        if not draft_dir.exists():
            return ()
        return tuple(
            DraftAssetEntry.model_validate(_read_json(path))
            for path in sorted(draft_dir.glob("*.json"))
        )

    def append_review(self, record: DraftAssetReviewRecord) -> Path:
        self.load_draft(record.draft_asset_id)
        path = self._review_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, record.model_dump(mode="json"))
        return path

    def load_reviews(self, draft_asset_id: str) -> tuple[DraftAssetReviewRecord, ...]:
        self.load_draft(draft_asset_id)
        review_dir = self.root / "reviews" / draft_asset_id
        if not review_dir.exists():
            return ()
        return tuple(
            DraftAssetReviewRecord.model_validate(_read_json(path))
            for path in sorted(review_dir.glob("*.json"))
        )

    def add_evidence(self, evidence: DraftAssetEvidenceRecord) -> Path:
        self.load_draft(evidence.draft_asset_id)
        path = self._evidence_path(evidence)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, evidence.model_dump(mode="json"))
        return path

    def load_evidence(self, draft_asset_id: str) -> tuple[DraftAssetEvidenceRecord, ...]:
        self.load_draft(draft_asset_id)
        evidence_dir = self.root / "evidence" / draft_asset_id
        if not evidence_dir.exists():
            return ()
        return tuple(
            DraftAssetEvidenceRecord.model_validate(_read_json(path))
            for path in sorted(evidence_dir.glob("*.json"))
        )

    def _draft_path(self, draft_asset_id: str) -> Path:
        return self._path_under_root("drafts", f"{_safe_draft_id(draft_asset_id)}.json")

    def _review_path(self, record: DraftAssetReviewRecord) -> Path:
        safe_timestamp = (
            record.reviewed_at.replace(":", "")
            .replace("+", "_")
            .replace("-", "")
            .replace(".", "_")
        )
        return (
            self._path_under_root(
                "reviews",
                _safe_draft_id(record.draft_asset_id),
                f"{safe_timestamp}_{record.decision.value}.json",
            )
        )

    def _evidence_path(self, evidence: DraftAssetEvidenceRecord) -> Path:
        return (
            self._path_under_root(
                "evidence",
                _safe_draft_id(evidence.draft_asset_id),
                f"{_safe_draft_id(evidence.evidence_id)}.json",
            )
        )

    def _path_under_root(self, *parts: str) -> Path:
        path = (self.root / Path(*parts)).resolve()
        if not path.is_relative_to(self.root):
            raise DraftQueueError(f"Draft queue path escapes root: {path}")
        return path


_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def _safe_draft_id(value: str) -> str:
    if not _SAFE_ID_RE.fullmatch(value):
        raise DraftQueueError(f"Invalid draft queue id: {value!r}")
    return value


def _read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


__all__ = ["DraftQueueError", "DraftReviewQueue"]
