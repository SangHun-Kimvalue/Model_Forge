from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import ValidationError

from modules.newbie_request.asset_draft_schemas import (
    DraftAssetEntry,
    DraftAssetEvidenceRecord,
    DraftAssetPrepareRecord,
    DraftAssetReviewRecord,
    PrefilterManualReviewDisposition,
    PrefilterManualReviewRecord,
    generated_draft_provenance_status,
)

# 리뷰 최신성 환산의 **정본**을 그대로 쓴다. 저장 경계가 자체 비교식을 갖게 두면
# "기록은 통과시켰는데 조회는 모호하다고 판정하는" 갈라짐이 생긴다 — 그러면 기록
# 시점 가드가 막으려던 상태가 다른 형태로 다시 새어나온다.
from modules.newbie_request.asset_registration import (
    ReviewOrderingError,
    review_instant,
)


class DraftQueueError(ValueError):
    """Raised when draft queue storage receives invalid or inconsistent data."""


class DraftPayloadConflictError(DraftQueueError):
    """같은 id에 **다른 payload**의 draft가 이미 온전히 게시돼 있다.

    :class:`CorruptDraftRecordError`와 구분되는 별도 타입인 이유: 운영자에게
    "이건 영구 충돌이니 저장하려는 쪽을 고쳐라"와 "디스크 레코드가 깨졌으니 복구하라"는
    서로 다른 조치다. 한 타입으로 뭉치면 그 구분이 사라진다.
    """


class CorruptDraftRecordError(DraftQueueError):
    """디스크의 draft 레코드를 파싱/검증할 수 없다 — **운영자 복구 대상**이다.

    ``"x"`` 모드는 파일 **생성**만 원자적이고 내용 게시까지 원자적이지는 않다. writer가
    쓰는 도중 죽으면 부분 JSON이 영구히 남고, ``draft_asset_id``가 content-addressed라
    그 draft는 **다시 저장될 방법이 없다**(매번 파싱 실패). 그러므로 이 상태를
    "재시도하면 된다"고 말해서는 안 된다 — 재시도는 복구가 아니다.

    **코드는 손상 파일을 자동으로 지우거나 덮지 않는다.** 동시에 쓰는 중인 writer의
    파일을 뺏을 수 있다. 모르면 통과시키지 않는다(fail-closed). 복구 절차는 형상마다
    다르므로 메시지가 형상별로 갈린다(:func:`_corrupt_record_message`).
    """


class DraftReviewQueue:
    """Repo-local/testable storage seam for draft asset review evidence."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def save_draft(self, draft: DraftAssetEntry) -> Path:
        """draft를 durable하게 저장한다 — **서로 다른 draft를 덮어쓰지 않는다**.

        옛 구현은 ``"w"``로 조용히 덮어썼다. ``draft_asset_id``가 프롬프트만의 함수이던
        시절, 같은 프롬프트의 두 번째 draft가 첫 번째의 ``generated_openscad_source``를
        지웠다. ``reviews/<id>/``의 리뷰 기록은 남는데 그 리뷰가 결속한 source는 디스크에서
        사라진다 — 실행은 Item 1의 대조가 차단하므로 **안전은 무너지지 않지만 사람이 승인한
        형상을 다시 만들 수 없다**. artifact-of-record가 성립하지 않는 것이다.
        (``append_review``·``save_manual_review``에 이미 고쳐진 것과 **같은 결함 클래스의
        세 번째 사례**다. 다만 ``_write_json_exclusive``를 그대로 쓰면 idempotent 재저장이
        깨지므로 세 갈래를 갖는 writer가 따로 필요하다.)

        **legacy 형상 generated draft는 전용 분기로 빠진다**(:meth:`_resave_legacy_draft`).
        나머지 전부(spec draft + 완전 provenance generated draft)가 아래 3갈래를 탄다:

        1. ``"x"`` exclusive create를 **먼저** 시도한다. ``exists()`` 선확인 후 쓰기는
           금지다 — 두 프로세스가 동시에 "없음"을 관측한다. ``"x"``는 존재확인과 생성이
           OS 수준 한 연산이다.
        2. 성공하면 payload를 쓰고 끝낸다.
        3. ``FileExistsError``면 기존 파일을 전부 다시 읽어 **canonical 재직렬화로**
           비교한다(바이트 비교 금지: 새 필드에 기본값이 있으므로 새 필드 이전에 저장된
           옛 JSON은 로드는 되지만 재직렬화하면 바이트가 달라진다 — 바이트로 비교하면
           논리적으로 동일한 draft가 충돌로 오판된다). 동일하면 idempotent no-op
           성공이고 **파일을 다시 쓰지 않는다**. 상이하면 fail-fast. 파싱 불가면
           "동일하다"고 판정하지 않는다(fail-closed).

        어느 실패 경로에서도 **디스크의 기존 파일은 한 바이트도 바뀌지 않는다.**

        주장한다: **데이터 손실이 없다** — 어떤 인터리빙에서도 이미 온전히 게시된 draft가
        다른 payload로 덮이지 않는다. 주장하지 **않는다**: 완전한 원자적 게시(temp 기록 후
        원자 게시 또는 lock)와 손상 레코드의 자동 복구. 그 둘은 이 슬라이스 범위 밖이고
        :class:`CorruptDraftRecordError`의 운영자 복구 절차로 정직하게 남긴다.
        """

        path = self._draft_path(draft.draft_asset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if _is_legacy_generated(draft):
            return self._resave_legacy_draft(draft, path)

        try:
            handle = path.open("x", encoding="utf-8")
        except FileExistsError as exc:
            existing = _canonical_draft_on_disk(path, legacy=False)
            if existing != _canonical_draft_text(draft):
                raise DraftPayloadConflictError(
                    f"A different draft is already stored at {path} and must not be "
                    f"overwritten (draft_asset_id={draft.draft_asset_id!r}). The stored "
                    "record is left untouched; drafts are artifacts of record."
                ) from exc
            return path  # idempotent no-op — 파일을 다시 쓰지 않는다.
        with handle:
            json.dump(draft.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            # 부분 파일이 남을 창을 최소화한다. draft 저장은 저빈도라 비용이 문제되지 않는다.
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _resave_legacy_draft(self, draft: DraftAssetEntry, path: Path) -> Path:
        """legacy 형상 draft는 **되살릴 수만 있고 만들어낼 수는 없다** (D9).

        스키마는 legacy 형상을 허용한다 — 그러지 않으면 새 필드 이전에 저장된 generated
        draft JSON이 로드 불가가 되고, 마이그레이션은 이 슬라이스 범위 밖이라 탈출구가
        없다. 그런데 스키마만 열어두면 새 호출자가 source만 넣고 provenance 다섯 필드를
        전부 비워 **legacy로 위장해 통과**할 수 있다 — provenance 누락이 "과거 레코드"로
        가장되는 정직성 결함이다. 그래서 스키마는 "디스크의 과거를 읽을 수 있게" 열어두고
        **이 저장 경계가 "과거를 새로 만들 수 없게" 닫는다.**

        3갈래(``"x"``)를 타지 않는 이유는 그 자체가 모순이기 때문이다 — ``"x"``가 성공하는
        순간 **새 legacy 파일이 만들어져** D9를 위반한다. 그래서 여기서는 파일을 **읽기
        모드로 직접 연다**(``exists()`` 선확인은 경쟁하고, 무엇보다 파일을 만들지 않는 것이
        핵심이다). 이 분기는 **어떤 경우에도 파일을 생성하지 않는다.**
        """

        try:
            handle = path.open("r", encoding="utf-8")
        except FileNotFoundError as exc:
            raise DraftQueueError(
                f"Refusing to create a new legacy-provenance draft at {path} "
                f"(draft_asset_id={draft.draft_asset_id!r}). A generated draft with all "
                "five SCAD provenance fields empty can only be a record written before "
                "those fields existed; legacy provenance is a read-only past, not a "
                "shape new callers may write. Author the draft with complete provenance "
                "(generator_provider / generator_model / scad_dsl / scad_origin / "
                "scad_cache_key) instead."
            ) from exc
        with handle:
            raw = handle.read()
        if _canonical_draft_from_text(raw, path, legacy=True) != _canonical_draft_text(draft):
            raise DraftPayloadConflictError(
                f"A different draft is already stored at {path} and must not be "
                f"overwritten (draft_asset_id={draft.draft_asset_id!r}). A legacy "
                "record may only be re-saved unchanged."
            )
        return path  # idempotent no-op — 파일을 열어보기만 했다.

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
        """Append a human review decision as a durable audit record.

        Uses the *exclusive* writer for the same reason ``save_manual_review``
        does: the file name is only the normalised ``reviewed_at`` plus the
        decision, so two records that share both — an injected fixed clock, a
        retry, two operators reviewing concurrently — would silently overwrite
        each other. An audit trail that can lose its own entries defeats the
        purpose of recording who decided what.

        The helper's own message names the *manual review* queue, which would
        misdirect an operator hitting a draft-review collision, so the error is
        re-raised in this context's own words.

        ⚠️ **같은 UTC instant의 판단이 이미 있으면 거부한다** (fallback ADR Item 5).
        ``_write_json_exclusive``는 *같은 파일명* 충돌만 막는데, 파일명은 정규화된
        ``reviewed_at`` + decision이라 **offset만 다른 같은 instant**
        (``10:00+09:00`` vs ``01:00+00:00``)는 서로 다른 파일이 되어 통과한다.
        그러면 "어느 쪽이 최신인지 알 수 없는" 모호한 상태가 큐에 남는다.

        **모호함은 조회 시점보다 기록 시점에 막는 것이 낫다.** 조회 시점에 터뜨리면
        모호한 draft 하나가 무관한 정상 후보까지 막는다(재사용 후보 탐색이 그렇다).

        ⚠️ **이 가드의 보장 범위는 "단일 프로세스 내 순차 append"까지다.**
        :meth:`_reject_ambiguous_instant`의 조회와 아래 ``_write_json_exclusive``는
        분리된 두 연산이라, 두 writer가 ``10:00+09:00 ACCEPT``와 ``01:00+00:00 REJECT``를
        **동시에** 기록하면 둘 다 통과할 수 있다(read-then-write 경쟁). 이 가드를
        **동시성 보장으로 읽지 말 것.** lock/reservation 파일은 동시성 인프라 신설이고,
        :meth:`save_draft`가 "완전한 원자적 게시는 범위 밖"으로 이미 NOT CLAIMED에
        남긴 것과 **정확히 같은 한계**다. 그 창으로 실제 모호 레코드가 생기면 읽는 쪽의
        격리(재사용 후보 탐색)와 fail-fast(등록·실행)가 받아낸다.
        """
        self.load_draft(record.draft_asset_id)
        self._reject_ambiguous_instant(record)
        path = self._review_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write_json_exclusive(path, record.model_dump(mode="json"))
        except DraftQueueError as exc:
            raise DraftQueueError(
                f"Draft review record already exists and must not be overwritten: {path}"
            ) from exc
        return path

    def _reject_ambiguous_instant(self, record: DraftAssetReviewRecord) -> None:
        """새 판단이 기존 판단과 **같은 instant**면 거부한다.

        새 레코드 자신의 ``reviewed_at``이 instant로 환산 불가면(비-ISO·naive) 그것도
        거부한다 — 순서를 알 수 없는 기록을 새로 만드는 것 역시 모호함의 생산이다.

        반면 **디스크의 기존 레코드**가 환산 불가면 건너뛴다. 그런 레코드 하나가 그
        draft에 대한 **새 리뷰 추가 자체를 영구히 막지 않게** 하기 위해서다.

        ⚠️ 그 이상을 주장하지 않는다: 새 리뷰를 **추가할 수 있을 뿐**, 그것으로 손상
        레코드가 치워지는 것은 **아니다**. 손상 레코드가 남아 있는 한
        :func:`current_review` 소비자(등록 게이트·fallback 실행)는 계속 fail-fast하므로
        그 draft는 여전히 등록·실행되지 않는다. **quarantine/repair 절차는 없다** —
        운영자가 파일을 직접 손봐야 한다.

        ⚠️ 이 조회와 실제 쓰기는 분리돼 있다 — 동시성 한계는 :meth:`append_review`
        docstring 참조.
        """

        instant = review_instant(record)
        for existing in self.load_reviews(record.draft_asset_id):
            try:
                existing_instant = review_instant(existing)
            except ReviewOrderingError:
                continue
            if existing_instant == instant:
                # 문구가 "Draft review record"로 시작하는 것은 계약이다: 이 예외는
                # exclusive-create 충돌보다 **먼저** 나므로, 운영자가 보는 첫 문장이
                # 여전히 *draft 리뷰* 레코드를 가리켜야 한다(manual-review 큐로
                # 오도되면 잘못된 레코드를 들여다본다).
                raise DraftQueueError(
                    "Draft review record for draft "
                    f"{record.draft_asset_id!r} is already recorded at the same "
                    f"instant ({instant.isoformat()}): existing "
                    f"{existing.decision.value!r} ({existing.reviewed_at!r}) vs new "
                    f"{record.decision.value!r} ({record.reviewed_at!r}). Which one "
                    "supersedes the other would not be decidable afterwards, so the "
                    "ambiguous state is refused here rather than left for every "
                    "reader to trip over. Record the new decision at a distinct time."
                )

    def load_reviews(self, draft_asset_id: str) -> tuple[DraftAssetReviewRecord, ...]:
        self.load_draft(draft_asset_id)
        review_dir = self.root / "reviews" / draft_asset_id
        if not review_dir.exists():
            return ()
        return tuple(
            DraftAssetReviewRecord.model_validate(_read_json(path))
            for path in sorted(review_dir.glob("*.json"))
        )

    def save_prepare(self, record: DraftAssetPrepareRecord) -> Path:
        self.load_draft(record.draft_asset_id)
        path = self._prepare_path(record.prepare_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_exclusive(path, record.model_dump(mode="json"))
        return path

    def load_prepare(self, prepare_id: str) -> DraftAssetPrepareRecord:
        path = self._prepare_path(prepare_id)
        if not path.is_file():
            raise DraftQueueError(f"Prepare record not found: {prepare_id}")
        return DraftAssetPrepareRecord.model_validate(_read_json(path))

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

    def save_manual_review(self, record: PrefilterManualReviewRecord) -> Path:
        """Persist a pre-filter manual-review request (fallback ADR item 6).

        Unlike the draft/review/evidence writers this uses an *exclusive*
        create: ``review_id`` is unique per request, not an idempotency key, so
        an id collision means two distinct audit records would land on the same
        file. Checking ``is_file()`` first would still lose one of them under a
        TOCTOU race, hence the atomic create-if-absent write.
        """
        path = self._manual_review_path(record.review_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_exclusive(path, record.model_dump(mode="json"))
        return path

    def load_manual_review(self, review_id: str) -> PrefilterManualReviewRecord:
        path = self._manual_review_path(review_id)
        if not path.is_file():
            raise DraftQueueError(f"Manual review record not found: {review_id}")
        return PrefilterManualReviewRecord.model_validate(_read_json(path))

    def list_manual_reviews(self) -> tuple[PrefilterManualReviewRecord, ...]:
        review_dir = self.root / "manual_reviews"
        if not review_dir.exists():
            return ()
        return tuple(
            PrefilterManualReviewRecord.model_validate(_read_json(path))
            for path in sorted(review_dir.glob("*.json"))
        )

    def save_manual_review_disposition(self, record: PrefilterManualReviewDisposition) -> Path:
        """사람이 내린 manual-review 처분을 감사 기록으로 영속한다.

        ``load_manual_review``를 먼저 부르는 이유(``append_review``가
        ``load_draft``를 부르는 선례와 같다): 원 요청 없는 처분은 **고아**이고,
        무엇에 대한 판단인지 되짚을 수 없는 감사 기록은 감사 기록이 아니다.

        exclusive-create인 이유: 경로는 ``review_id`` 하나로 결정되므로 두 번째
        처분이 조용히 첫 처분을 덮으면 **이미 내려진 IP 판정이 흔적 없이 교체**
        된다. 처분 정정은 supersede 레코드를 갖춘 별도 설계이고, 그때까지는
        "실패하되 시끄럽게"가 옳다.

        helper가 고정 생성하는 문구는 *요청* 레코드 충돌을 가리키므로 그대로
        흘리면 운영자가 잘못된 레코드를 들여다본다. ``append_review``의 선례대로
        이 문맥의 말로 번역해 재발생시킨다.
        """
        self.load_manual_review(record.review_id)
        path = self._manual_review_disposition_path(record.review_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write_json_exclusive(path, record.model_dump(mode="json"))
        except DraftQueueError as exc:
            raise DraftQueueError(
                f"Manual review disposition already exists and must not be overwritten: {path}"
            ) from exc
        return path

    def load_manual_review_disposition(self, review_id: str) -> PrefilterManualReviewDisposition:
        path = self._manual_review_disposition_path(review_id)
        if not path.is_file():
            raise DraftQueueError(f"Manual review disposition not found: {review_id}")
        return PrefilterManualReviewDisposition.model_validate(_read_json(path))

    def list_manual_review_dispositions(
        self,
    ) -> tuple[PrefilterManualReviewDisposition, ...]:
        disposition_dir = self.root / "manual_review_dispositions"
        if not disposition_dir.exists():
            return ()
        return tuple(
            PrefilterManualReviewDisposition.model_validate(_read_json(path))
            for path in sorted(disposition_dir.glob("*.json"))
        )

    def _manual_review_disposition_path(self, review_id: str) -> Path:
        return self._path_under_root(
            "manual_review_dispositions", f"{_safe_draft_id(review_id)}.json"
        )

    def _manual_review_path(self, review_id: str) -> Path:
        return self._path_under_root("manual_reviews", f"{_safe_draft_id(review_id)}.json")

    def _draft_path(self, draft_asset_id: str) -> Path:
        return self._path_under_root("drafts", f"{_safe_draft_id(draft_asset_id)}.json")

    def _prepare_path(self, prepare_id: str) -> Path:
        if not re.fullmatch(r"prepare_[0-9a-f]{32}", prepare_id):
            raise DraftQueueError(f"Invalid prepare id: {prepare_id!r}")
        return self._path_under_root("prepares", f"{prepare_id}.json")

    def _review_path(self, record: DraftAssetReviewRecord) -> Path:
        safe_timestamp = (
            record.reviewed_at.replace(":", "").replace("+", "_").replace("-", "").replace(".", "_")
        )
        return self._path_under_root(
            "reviews",
            _safe_draft_id(record.draft_asset_id),
            f"{safe_timestamp}_{record.decision.value}.json",
        )

    def _evidence_path(self, evidence: DraftAssetEvidenceRecord) -> Path:
        return self._path_under_root(
            "evidence",
            _safe_draft_id(evidence.draft_asset_id),
            f"{_safe_draft_id(evidence.evidence_id)}.json",
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


def _is_legacy_generated(draft: DraftAssetEntry) -> bool:
    """저장 경계에서 **legacy 전용 분기**를 타야 하는 draft인가.

    spec draft는 정본 분류 helper를 부르기 **전에** 갈라낸다. spec도 provenance 다섯
    필드가 전부 ``None``이라 helper의 "generated 전용" 전조건을 위반하고, 무엇보다
    spec은 3갈래를 정상적으로 타야 한다(spec draft는 얼마든지 새로 만들어진다).
    판정 자체는 helper가 소유한다 — 여기서 None 개수를 다시 세면 판정이 두 벌이 된다.
    """

    if draft.generated_openscad_source is None:
        return False
    return generated_draft_provenance_status(draft) == "legacy"


def _canonical_draft_text(entry: DraftAssetEntry) -> str:
    """비교 정본 — 논리적 동등성은 **canonical 재직렬화**로만 판정한다."""

    return json.dumps(
        entry.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _corrupt_record_message(path: Path, *, legacy: bool) -> str:
    """손상 레코드의 운영자 안내 — **복구 절차는 형상마다 다르다**.

    공통 문구 하나로 뭉개면 안 된다: legacy draft는 삭제해도 복구되지 않으므로
    ("삭제 후 재시도"를 안내하면) **실패가 확정된 절차를 운영자에게 안내**하게 된다.
    """

    if legacy:
        recovery = (
            "Recovery: restore a valid past record at this exact path, then re-save "
            "the identical entry. Deleting the file does NOT recover it - a new "
            "legacy-provenance draft cannot be created, so every later save would be "
            "refused. A legacy record can be revived, never manufactured."
        )
    else:
        recovery = (
            "Recovery: delete this file and retry. A partial file never held a "
            "complete record, so deleting it loses nothing."
        )
    return (
        f"Draft record at {path} could not be parsed or validated. This is NOT a "
        "conflict with a different draft, and it is not automatically recoverable: "
        "the file is left exactly as found (never deleted or overwritten by this "
        f"code, which could steal a concurrent writer's file). {recovery}"
    )


def _canonical_draft_from_text(raw: str, path: Path, *, legacy: bool) -> str:
    try:
        payload = json.loads(raw)
        entry = DraftAssetEntry.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        # 파싱/검증 실패가 호출자에게 그대로 새어나가면 운영자 복구 계약이 깨진다.
        raise CorruptDraftRecordError(_corrupt_record_message(path, legacy=legacy)) from exc
    return _canonical_draft_text(entry)


def _canonical_draft_on_disk(path: Path, *, legacy: bool) -> str:
    """디스크 레코드를 canonical 형태로 읽는다.

    ``"x"``가 이미 "존재한다"고 알려준 뒤 부르므로 ``FileNotFoundError``는 나지 않는
    것이 정상이지만, 그 사이에 사라졌다면 그것도 "모르는 상태"다 — 손상 계약으로
    변환한다(fail-closed).
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptDraftRecordError(_corrupt_record_message(path, legacy=legacy)) from exc
    return _canonical_draft_from_text(raw, path, legacy=legacy)


def _read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _write_json_exclusive(path: Path, payload: object) -> None:
    """Write JSON only if the file does not exist yet, atomically.

    ``"x"`` makes the existence check and the create one operation at the OS
    level, so a concurrent writer cannot overwrite an audit record that another
    request just created. Used by the audit-record writers — ``save_manual_review``
    and ``append_review``; ``save_draft``/``add_evidence`` keep their ``"w"``
    semantics unchanged.

    The ``DraftQueueError`` message below is worded for the manual-review path.
    ``append_review`` catches it and re-raises in its own wording, so callers of
    other queues are not told to look at the wrong record type.
    """
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise DraftQueueError(
            f"Manual review record already exists and must not be overwritten: {path}"
        ) from exc
    with handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


__all__ = [
    "CorruptDraftRecordError",
    "DraftPayloadConflictError",
    "DraftQueueError",
    "DraftReviewQueue",
]
