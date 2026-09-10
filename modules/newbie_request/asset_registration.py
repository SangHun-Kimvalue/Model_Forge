from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.asset_catalog import (
    DecorativeAssetLegalReviewStatus,
    DecorativeAssetPrintabilityStatus,
)
from modules.newbie_request.asset_draft_schemas import (
    AssetLifecycleStatus,
    DraftAssetEntry,
    DraftAssetPrepareRecord,
    DraftAssetReviewRecord,
    DraftReviewDecision,
    generated_source_sha256,
)
from modules.newbie_request.printability import PrintabilityReport, PrintabilityStatus
from modules.newbie_request.schemas import VisualQualityStatus

_NEXT_GATES = (
    "visual_manual_pass",
    "legal_review_approved",
    "printability_smoke_passed",
    "runtime_catalog_registration",
)


class ReviewOrderingError(ValueError):
    """리뷰의 **최신성을 판정할 수 없다** — 조용히 하나를 고르지 않는다.

    이 예외가 발생하는 세 형상은 전부 "모른다"이지 "괜찮다"가 아니다:
    ``reviewed_at``이 ISO 8601이 아니거나, timezone이 없어 instant로 환산할 수 없거나,
    **같은 instant에 판단이 둘**이라 어느 쪽이 최신인지 알 수 없다.

    보조키(파일명·decision·reviewer)로 뭉개지 않는 이유: 이 판정의 소비자는 등록
    게이트와 **fallback 실행 허용 경계**다. 임의 tiebreak은 곧 "REJECT와 ACCEPT가
    같은 시각에 있을 때 실행 허용 여부를 동전던지기로 정한다"가 된다. 시끄럽게 멈추는
    쪽이 fail-closed다.

    ⚠️ **이 예외를 어디서 터뜨리느냐는 소비자마다 다르다.** 모호한 draft **자신의**
    권한 경계(등록 게이트·fallback 실행)에서는 멈추는 것이 옳다. 반면 draft 재사용
    **후보 탐색**은 여러 draft를 훑으므로, 여기서 터뜨리면 모호한 draft 하나가 무관한
    정상 후보까지 막는다 — 그쪽은 해당 후보만 배제하고 카운터로 드러낸다
    (``modules.newbie_request.asset_intake``). 모호한 상태의 유입을 줄이는 주 경계는
    :meth:`DraftReviewQueue.append_review`이지만, 그 가드는 **단일 프로세스 내 순차
    append 기준**이라 동시 기록까지 막지는 않는다 — 그래서 읽는 쪽의 이 처리가
    없어도 되는 것이 아니다.
    """


def review_instant(record: DraftAssetReviewRecord) -> datetime:
    """``reviewed_at``을 **UTC instant**로 환산한다 — 이 환산의 정본.

    공개인 이유: 저장 경계(:meth:`DraftReviewQueue.append_review`)가 **기록 시점에**
    같은 instant의 판단이 이미 있는지 물어야 한다. 그 비교를 저장 경계가 자체 구현하면
    "기록은 통과시켰는데 조회는 모호하다고 판정하는" 갈라짐이 생긴다.

    문자열 ``max()``를 쓰지 않는 이유(잠복 버그였다): ``record_draft_review``는 임의
    offset의 ``datetime``을 받아 ``isoformat()`` 그대로 저장하므로,
    ``2026-07-29T10:00:00+09:00``(=UTC 01:00) ACCEPT 뒤에 **실제로 더 늦은**
    ``2026-07-29T02:00:00+00:00`` REJECT를 기록해도 문자열 비교는 ``"10..." > "02..."``
    라 ACCEPT를 최신으로 본다 — 거부된 형상이 실행/재사용으로 돌아온다.
    """

    try:
        parsed = datetime.fromisoformat(record.reviewed_at)
    except ValueError as exc:
        raise ReviewOrderingError(
            f"Review record for draft {record.draft_asset_id!r} carries a "
            f"non-ISO-8601 reviewed_at ({record.reviewed_at!r}); review recency "
            "cannot be decided, so no review is treated as the latest."
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ReviewOrderingError(
            f"Review record for draft {record.draft_asset_id!r} carries a "
            f"timezone-naive reviewed_at ({record.reviewed_at!r}); it cannot be "
            "converted to an instant without inventing a timezone, and guessing "
            "one would silently reorder reviews recorded from other offsets."
        )
    return parsed.astimezone(UTC)


def record_draft_review(
    draft: DraftAssetEntry,
    *,
    decision: DraftReviewDecision,
    reviewer: str,
    review_note: str,
    prepare: DraftAssetPrepareRecord | None = None,
    visual_quality_status: VisualQualityStatus | None = None,
    legal_review_status: DecorativeAssetLegalReviewStatus | None = None,
    commercial_allowed: bool | None = None,
    redistribution_allowed: bool | None = None,
    modification_allowed: bool | None = None,
    reviewed_at: datetime | None = None,
) -> DraftAssetReviewRecord:
    """검토 판단을 기록한다 — **무엇을 검토했는지까지** 함께 못박는다.

    ``reviewed_source_sha256``이 **파라미터가 아닌** 이유: 이 함수는 이미 ``draft``를
    받으므로 여기서 계산하면 호출자가 빠뜨리거나 다른 값으로 바꿔치기할 수 없다.
    인자로 노출하는 순간 "리뷰 대상이 아닌 digest"를 넣을 수 있고, 그러면 이 결속은
    기록만 있고 강제력이 없는 장식이 된다. 계산은 실행 경로와 **같은 함수**
    (:func:`generated_source_sha256`)로 한다 — 두 벌이면 영원히 불일치한다.

    spec 기반 draft(``generated_openscad_source is None``)는 결속할 source가 없으므로
    ``None``이 정상이다.
    """

    reviewed_at = reviewed_at or datetime.now(UTC)
    lifecycle = _lifecycle_from_decision(decision)
    source = draft.generated_openscad_source
    return DraftAssetReviewRecord(
        draft_asset_id=draft.draft_asset_id,
        decision=decision,
        asset_lifecycle_status=lifecycle,
        reviewer=reviewer,
        reviewed_at=reviewed_at.isoformat(),
        review_note=review_note,
        required_next_gates=_NEXT_GATES,
        reviewed_source_sha256=(generated_source_sha256(source) if source is not None else None),
        reviewed_artifact_sha256=prepare.stl_sha256 if prepare is not None else None,
        reviewed_prepare_id=prepare.prepare_id if prepare is not None else None,
        visual_quality_status=visual_quality_status,
        legal_review_status=legal_review_status,
        commercial_allowed=commercial_allowed,
        redistribution_allowed=redistribution_allowed,
        modification_allowed=modification_allowed,
        release_allowed=False,
        runtime_catalog_registered=False,
    )


class RegistrationGateStatus(StrEnum):
    BLOCKED = "registration_blocked"
    CANDIDATE = "registration_candidate"


class RegistrationBlockReason(StrEnum):
    NO_ACCEPT_REVIEW = "no_accept_review"
    LATEST_REVIEW_NOT_ACCEPT = "latest_review_not_accept"
    MISSING_MANUAL_VISUAL_PASS = "missing_manual_visual_pass"
    LEGAL_NOT_APPROVED = "legal_not_approved"
    PRINTABILITY_NOT_PASSED = "printability_not_passed"
    PRINTABILITY_EVIDENCE_MISSING = "printability_evidence_missing"
    PRINTABILITY_EVIDENCE_NOT_PASSED = "printability_evidence_not_passed"
    RIGHTS_NOT_VALID = "rights_not_valid"
    RELEASE_NOT_ALLOWED = "release_not_allowed"
    RUNTIME_CATALOG_ALREADY_REGISTERED = "runtime_catalog_already_registered"


class RegistrationGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft: DraftAssetEntry
    review_records: tuple[DraftAssetReviewRecord, ...] = ()
    visual_quality_status: VisualQualityStatus
    legal_review_status: DecorativeAssetLegalReviewStatus
    printability_status: DecorativeAssetPrintabilityStatus
    printability_report: PrintabilityReport | None = None
    commercial_allowed: bool
    redistribution_allowed: bool
    modification_allowed: bool
    release_allowed: bool = False
    require_release_allowed: bool = False

    @model_validator(mode="after")
    def _records_belong_to_draft(self) -> RegistrationGateRequest:
        for record in self.review_records:
            if record.draft_asset_id != self.draft.draft_asset_id:
                raise ValueError("review record does not belong to draft")
        return self


class RegistrationGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RegistrationGateStatus
    draft_asset_id: str
    blocked_reasons: tuple[RegistrationBlockReason, ...] = ()
    release_allowed: bool = False
    runtime_catalog_registered: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def is_candidate(self) -> bool:
        return self.status is RegistrationGateStatus.CANDIDATE


def evaluate_registration_gate(
    request: RegistrationGateRequest,
) -> RegistrationGateResult:
    reasons: list[RegistrationBlockReason] = []

    latest_review = current_review(request.review_records)
    if latest_review is None:
        reasons.append(RegistrationBlockReason.NO_ACCEPT_REVIEW)
    elif latest_review.decision is not DraftReviewDecision.ACCEPT:
        reasons.append(RegistrationBlockReason.LATEST_REVIEW_NOT_ACCEPT)
    if request.visual_quality_status is not VisualQualityStatus.MANUAL_PASS:
        reasons.append(RegistrationBlockReason.MISSING_MANUAL_VISUAL_PASS)
    if request.legal_review_status is not DecorativeAssetLegalReviewStatus.APPROVED:
        reasons.append(RegistrationBlockReason.LEGAL_NOT_APPROVED)
    if request.printability_status is not DecorativeAssetPrintabilityStatus.SMOKE_PASSED:
        reasons.append(RegistrationBlockReason.PRINTABILITY_NOT_PASSED)
    elif request.printability_report is None:
        reasons.append(RegistrationBlockReason.PRINTABILITY_EVIDENCE_MISSING)
    elif (
        request.printability_report.status is not PrintabilityStatus.SMOKE_PASSED
        or not request.printability_report.can_slice
    ):
        reasons.append(RegistrationBlockReason.PRINTABILITY_EVIDENCE_NOT_PASSED)
    if not (
        request.commercial_allowed
        and request.redistribution_allowed
        and request.modification_allowed
    ):
        reasons.append(RegistrationBlockReason.RIGHTS_NOT_VALID)
    if request.require_release_allowed and not request.release_allowed:
        reasons.append(RegistrationBlockReason.RELEASE_NOT_ALLOWED)
    if request.draft.runtime_catalog_registered:
        reasons.append(RegistrationBlockReason.RUNTIME_CATALOG_ALREADY_REGISTERED)

    if reasons:
        return RegistrationGateResult(
            status=RegistrationGateStatus.BLOCKED,
            draft_asset_id=request.draft.draft_asset_id,
            blocked_reasons=tuple(dict.fromkeys(reasons)),
            release_allowed=False,
            runtime_catalog_registered=False,
            metadata=_gate_metadata(request),
        )

    return RegistrationGateResult(
        status=RegistrationGateStatus.CANDIDATE,
        draft_asset_id=request.draft.draft_asset_id,
        blocked_reasons=(),
        release_allowed=False,
        runtime_catalog_registered=False,
        metadata=_gate_metadata(request),
    )


def current_review(
    records: tuple[DraftAssetReviewRecord, ...],
) -> DraftAssetReviewRecord | None:
    """리뷰 기록들 중 **최신 판단** — 이 정책의 정본 (fallback ADR Item 5).

    공개 helper인 이유: 소비자가 셋이다 — 등록 게이트
    (:func:`evaluate_registration_gate`), fallback **실행 허용 경계**
    (``apps.orchestrator.fallback_draft_execution``), 그리고 draft 재사용의 리뷰
    투영(``modules.newbie_request.asset_intake``). 각자 ``max(...)``를 쓰면 정책이
    세 벌이 되어 "등록은 거부하는데 실행은 허용하는" 조용한 갈라짐이 생긴다.

    Raises:
        ReviewOrderingError: 최신성을 판정할 수 없을 때(파싱 불가·naive·동일 instant).
            ``None``(=리뷰 없음)으로 뭉개지 않는다 — 소비자에게 "리뷰가 없다"와
            "어느 리뷰가 최신인지 모른다"는 완전히 다른 사실이다.
    """

    if not records:
        return None
    dated = tuple((review_instant(record), record) for record in records)
    latest_instant = max(instant for instant, _ in dated)
    tied = tuple(record for instant, record in dated if instant == latest_instant)
    if len(tied) > 1:
        raise ReviewOrderingError(
            f"Draft {tied[0].draft_asset_id!r} has {len(tied)} review records at "
            f"the same latest instant ({latest_instant.isoformat()}): "
            f"{sorted(record.decision.value for record in tied)}. Which one "
            "supersedes the other is not decidable, and picking one by a "
            "secondary key would be a silent verdict."
        )
    return tied[0]


def _gate_metadata(request: RegistrationGateRequest) -> dict[str, object]:
    return {
        "review_count": len(request.review_records),
        "visual_quality_status": request.visual_quality_status.value,
        "legal_review_status": request.legal_review_status.value,
        "printability_status": request.printability_status.value,
        "printability_evidence_status": request.printability_report.status.value
        if request.printability_report is not None
        else None,
        "printability_can_slice": request.printability_report.can_slice
        if request.printability_report is not None
        else None,
        "commercial_allowed": request.commercial_allowed,
        "redistribution_allowed": request.redistribution_allowed,
        "modification_allowed": request.modification_allowed,
        "release_allowed_input": request.release_allowed,
        "require_release_allowed": request.require_release_allowed,
        "draft_generator": request.draft.draft_generator,
    }


def _lifecycle_from_decision(decision: DraftReviewDecision) -> AssetLifecycleStatus:
    if decision is DraftReviewDecision.REJECT:
        return AssetLifecycleStatus.REJECTED
    if decision is DraftReviewDecision.CANDIDATE:
        return AssetLifecycleStatus.REVIEW_CANDIDATE
    return AssetLifecycleStatus.ACCEPTED_FOR_REGISTRATION


__all__ = [
    "RegistrationBlockReason",
    "RegistrationGateRequest",
    "RegistrationGateResult",
    "RegistrationGateStatus",
    "ReviewOrderingError",
    "current_review",
    "review_instant",
    "evaluate_registration_gate",
    "record_draft_review",
]
