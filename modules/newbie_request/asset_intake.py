from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.asset_catalog import (
    AssetCandidateExplanation,
    AssetCandidateExplanationSource,
    AssetCandidateMatchLevel,
    DecorativeAssetSelectionRequest,
    DecorativeAssetSelectionStatus,
    DecorativeAssetSelector,
    DecorativeAssetSourceType,
)
from modules.newbie_request.asset_draft_schemas import (
    DraftAssetEntry,
    DraftAssetReviewRecord,
    DraftReviewDecision,
    ScadGeneratorIdentity,
    generated_draft_provenance_status,
)
from modules.newbie_request.asset_registration import (
    ReviewOrderingError,
    current_review,
)
from modules.newbie_request.draft_queue import DraftReviewQueue

_SAFE_PUBLIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


class AssetIntakeError(ValueError):
    """Raised when asset intake receives unsafe identifiers or paths."""


class AssetIntakeStatus(StrEnum):
    RUNTIME_CATALOG_MATCH = "runtime_catalog_match"
    DRAFT_QUEUE_MATCH = "draft_queue_match"
    DUPLICATE_OR_SIMILAR_CANDIDATE = "duplicate_or_similar_candidate"
    NEW_DRAFT_ALLOWED = "new_draft_allowed"


class AssetIntakeReason(StrEnum):
    RUNTIME_EXACT_MATCH = "runtime_exact_match"
    EXISTING_DRAFT_MATCH = "existing_draft_match"
    SIMILAR_REFERENCE_CANDIDATE = "similar_reference_candidate"
    NO_EXISTING_ASSET_OR_DRAFT = "no_existing_asset_or_draft"


class DraftMatchBasis(StrEnum):
    """draft가 **무엇으로** 요청과 맞았는가 (fallback ADR Item 5 E2).

    두 basis는 서로 다른 질문에 답한다:

    - :attr:`SCAD_CACHE_KEY` = "같은 SCAD 생성 job인가" — 프롬프트 원문 + DSL +
      생성기/버전 + provider + model. generated draft는 **이것만** 후보가 된다.
      다른 모델이 만든 형상을 재사용하면서 "이 모델로 만들었다"는 provenance를 달면
      거짓 주장이 되기 때문이다.
    - :attr:`SPEC_IDENTITY` = "같은 자산 요청인가" — subject/category/style.
      결정론 생성기 산출(spec draft)은 캐시 키가 **구조적으로 없고**(D8 불변식) 생성이
      재현 가능하므로 메타데이터 일치로 충분하다.
    """

    SCAD_CACHE_KEY = "scad_cache_key"
    SPEC_IDENTITY = "spec_identity"


class DraftReviewProjection(StrEnum):
    """후보 draft에 **사람이 내린 최신 판단**의 투영 (fallback ADR Item 5 E3).

    ⚠️ ``DraftAssetEntry.asset_lifecycle_status``로는 이 값을 알 수 없다 —
    ``_draft_starts_closed`` validator가 저장된 draft의 그 필드를 **항상 ``DRAFT``로
    강제**하므로 그것은 상수이지 사람의 판단이 아니다. 판단은 별도 리뷰 레코드에
    살고, ``list_drafts()``는 리뷰를 결합하지 않는다. 그래서 후보마다
    ``load_reviews()``를 조인해야만 REJECT를 걸러낼 수 있다.
    """

    ACCEPTED = "accepted"
    CANDIDATE = "candidate"
    UNREVIEWED = "unreviewed"
    REJECTED = "rejected"


#: 리뷰 투영 우선순위 — 작을수록 먼저 뽑힌다. ``REJECTED``는 정렬 전에 제거되므로
#: 여기에 없다(순위를 주면 "가장 나쁜 걸 고를 수도 있다"고 읽힌다).
_REVIEW_PROJECTION_RANK: dict[DraftReviewProjection, int] = {
    DraftReviewProjection.ACCEPTED: 0,
    DraftReviewProjection.CANDIDATE: 1,
    DraftReviewProjection.UNREVIEWED: 2,
}

#: match_basis 우선순위 — 리뷰 투영 **다음**이다(E2b).
_MATCH_BASIS_RANK: dict[DraftMatchBasis, int] = {
    DraftMatchBasis.SCAD_CACHE_KEY: 0,
    DraftMatchBasis.SPEC_IDENTITY: 1,
}

_DECISION_PROJECTION: dict[DraftReviewDecision, DraftReviewProjection] = {
    DraftReviewDecision.ACCEPT: DraftReviewProjection.ACCEPTED,
    DraftReviewDecision.CANDIDATE: DraftReviewProjection.CANDIDATE,
    DraftReviewDecision.REJECT: DraftReviewProjection.REJECTED,
}


class DraftMatchSelection(BaseModel):
    """draft 재사용 **선택 결과 + 그 근거** (fallback ADR Item 5 E7).

    ``DraftAssetEntry | None``만 돌려주면 "왜 이게 뽑혔는지"와 "무엇이 배제됐는지"가
    호출부에서 사라진다. 운영자가 재사용 결정을 감사하려면 배제 전 후보 수와 REJECT
    배제 수가 함께 보여야 한다 — 특히 ``rejected_excluded_count``가 0이 아닌데
    재사용이 일어났다면 그건 사람이 알아야 할 사실이다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected: DraftAssetEntry | None = None
    match_basis: DraftMatchBasis | None = None
    review_projection: DraftReviewProjection | None = None
    #: **배제 전** 전체 매칭 수(두 basis 갈래의 합). 선택된 basis의 후보 수가 아니다.
    candidate_count: int = Field(default=0, ge=0)
    rejected_excluded_count: int = Field(default=0, ge=0)
    #: 리뷰 최신성을 판정할 수 없어(동일 instant·손상 레코드) 배제된 후보 수.
    #: 0이 아니면 큐에 **사람이 고쳐야 할 레코드**가 있다는 뜻이다 — 조용히 배제하면
    #: 운영자가 그 존재를 영영 모른다.
    ambiguous_excluded_count: int = Field(default=0, ge=0)


class ReferenceAssetCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    subject: str = Field(min_length=1)
    category: str = Field(min_length=1)
    style: str | None = None
    source_type: DecorativeAssetSourceType
    license: str = Field(min_length=1)
    source_url_or_owner: str = Field(min_length=1)
    provenance_note: str = Field(min_length=1)
    commercial_allowed: bool
    redistribution_allowed: bool
    modification_allowed: bool

    @model_validator(mode="after")
    def _reference_must_be_rights_clear(self) -> ReferenceAssetCandidate:
        if not (
            self.commercial_allowed
            and self.redistribution_allowed
            and self.modification_allowed
        ):
            raise ValueError("reference candidates must have rights metadata cleared")
        return self


class AssetIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_prompt_ko: str = Field(min_length=1)
    request_id: str | None = None
    subject: str | None = None
    category: str | None = None
    style: str | None = None


class AssetIntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AssetIntakeStatus
    reason: AssetIntakeReason
    runtime_asset_id: str | None = None
    draft_asset_id: str | None = None
    draft_asset: DraftAssetEntry | None = None
    candidate_asset_ids: tuple[str, ...] = ()
    reference_candidate_ids: tuple[str, ...] = ()
    new_draft_allowed: bool
    metadata: dict[str, object] = Field(default_factory=dict)
    candidate_explanations: tuple[AssetCandidateExplanation, ...] = ()


class AssetFolderPolicy:
    """Path policy for future asset library storage.

    This class only computes safe paths. It does not create folders and does not
    mutate the runtime catalog.
    """

    def __init__(self, asset_root: str | Path) -> None:
        self.asset_root = Path(asset_root).resolve()

    def runtime_asset_path(self, asset_id: str) -> Path:
        return self._path_under_root("runtime", _safe_public_id(asset_id), "asset.json")

    def draft_asset_path(self, draft_asset_id: str) -> Path:
        return self._path_under_root(
            "drafts",
            _safe_public_id(draft_asset_id),
            "draft_asset.json",
        )

    def draft_review_path(self, draft_asset_id: str, review_id: str) -> Path:
        return self._path_under_root(
            "drafts",
            _safe_public_id(draft_asset_id),
            "reviews",
            f"{_safe_public_id(review_id)}.json",
        )

    def reference_note_path(self, reference_id: str) -> Path:
        return self._path_under_root(
            "references",
            _safe_public_id(reference_id),
            "reference_note.json",
        )

    def _path_under_root(self, *parts: str) -> Path:
        path = (self.asset_root / Path(*parts)).resolve()
        if not path.is_relative_to(self.asset_root):
            raise AssetIntakeError(f"Asset folder path escapes root: {path}")
        return path


class AssetIntakeResolver:
    """Deterministic intake gate before creating a new draft asset."""

    def __init__(
        self,
        runtime_selector: DecorativeAssetSelector,
        *,
        draft_queue: DraftReviewQueue | None = None,
        generator_identity: ScadGeneratorIdentity | None = None,
        reference_candidates: tuple[ReferenceAssetCandidate, ...] = (),
    ) -> None:
        self._runtime_selector = runtime_selector
        self._draft_queue = draft_queue
        # 생성기 정체성이 없으면 요청 측 캐시 키를 계산할 수 없다 → generated draft
        # 재사용을 **하지 않는다**(spec draft 매칭은 정상 동작). 무엇으로 생성할지
        # 모르면서 "같은 job"이라고 판정할 수 없으므로 fail-closed로 옳다.
        self._generator_identity = generator_identity
        self._reference_candidates = reference_candidates

    @property
    def draft_queue(self) -> DraftReviewQueue | None:
        """주입된 큐 (배선을 단언할 수 있도록 노출 — ``prefilter`` 선례와 같다)."""
        return self._draft_queue

    @property
    def generator_identity(self) -> ScadGeneratorIdentity | None:
        """주입된 생성기 정체성 snapshot (배선 단언용)."""
        return self._generator_identity

    def resolve(self, request: AssetIntakeRequest) -> AssetIntakeResult:
        runtime_selection = self._runtime_selector.select(
            DecorativeAssetSelectionRequest(
                request_id=request.request_id,
                user_prompt_ko=request.user_prompt_ko,
                subject=request.subject,
                category=request.category,
                style=request.style,
            )
        )
        if runtime_selection.status is DecorativeAssetSelectionStatus.SELECTED:
            return AssetIntakeResult(
                status=AssetIntakeStatus.RUNTIME_CATALOG_MATCH,
                reason=AssetIntakeReason.RUNTIME_EXACT_MATCH,
                runtime_asset_id=runtime_selection.asset_id,
                candidate_asset_ids=runtime_selection.candidate_asset_ids,
                new_draft_allowed=False,
                candidate_explanations=runtime_selection.candidate_explanations,
                metadata={
                    "request_id": runtime_selection.request_id,
                    "subject": runtime_selection.subject,
                    "category": runtime_selection.category,
                    "style": runtime_selection.style,
                },
            )

        selection = self._matching_draft(request)
        draft = selection.selected
        if draft is not None:
            return AssetIntakeResult(
                status=AssetIntakeStatus.DRAFT_QUEUE_MATCH,
                reason=AssetIntakeReason.EXISTING_DRAFT_MATCH,
                draft_asset_id=draft.draft_asset_id,
                draft_asset=draft,
                new_draft_allowed=False,
                candidate_explanations=(
                    _draft_queue_explanation(draft),
                ),
                metadata=_draft_match_metadata(selection, draft),
            )

        reference_candidates = self._matching_references(request)
        if reference_candidates:
            return AssetIntakeResult(
                status=AssetIntakeStatus.DUPLICATE_OR_SIMILAR_CANDIDATE,
                reason=AssetIntakeReason.SIMILAR_REFERENCE_CANDIDATE,
                reference_candidate_ids=tuple(
                    candidate.reference_id for candidate in reference_candidates
                ),
                new_draft_allowed=False,
                candidate_explanations=_reference_candidate_explanations(
                    reference_candidates
                ),
                metadata={
                    "subject": request.subject,
                    "category": request.category,
                    "style": request.style,
                    **_selection_counters(selection),
                },
            )

        return AssetIntakeResult(
            status=AssetIntakeStatus.NEW_DRAFT_ALLOWED,
            reason=AssetIntakeReason.NO_EXISTING_ASSET_OR_DRAFT,
            new_draft_allowed=True,
            candidate_explanations=(
                _new_draft_explanation(request),
            ),
            metadata={
                "subject": request.subject,
                "category": request.category,
                "style": request.style,
                # ⚠️ 카운터는 **매칭에 실패한 경로에도** 실린다. 여기 없으면
                # "후보가 있었는데 전부 배제돼서 새로 만든다"와 "후보가 아예 없어서
                # 새로 만든다"가 사용자 흐름에서 **구별되지 않는다** — 손상된 옛 draft가
                # 조용히 배제되고 같은 요청으로 새 draft가 만들어지는데, 그 사실이
                # 어디에도 안 남는다. 그건 이 카운터가 막으려던 바로 그 상황이다.
                **_selection_counters(selection),
            },
        )

    def _matching_draft(self, request: AssetIntakeRequest) -> DraftMatchSelection:
        """재사용 후보를 고른다 — **배제 후 정렬 후 첫 번째** (E2·E3).

        옛 구현은 ``matches[0]``이었다. 그건 "한 job = 한 파일"일 때만 말이 됐는데,
        draft id가 artifact-addressed가 된 뒤로는 반복 요청이 draft를 **누적**하므로
        파일명 정렬 순의 임의 선택이 됐다. 게다가 사람이 **REJECT**한 형상도 후보에
        들어 있어서, 거부된 형상이 재사용으로 돌아오면 human-review 계약이 뒤집힌다.

        전체 정렬 키는 ``(리뷰 투영, match_basis, draft_asset_id)``다. 리뷰가 basis보다
        **먼저**인 이유(E2b): 생성은 비결정론적이라 "같은 캐시 키"는 *같은 job*을 뜻할 뿐
        *같은 결과물*을 뜻하지 않는다(같은 키의 서로 다른 artifact가 여럿 존재할 수 있고,
        그것이 id를 artifact-addressed로 바꾼 이유다). 반면 리뷰는 **artifact 그 자체**에
        대한 판단이고 이 제품에서 그 권위는 사람에게 있다(DESIGN.md R17). 그래서
        ACCEPT된 spec draft가 미검토 generated draft보다 앞선다.
        """

        queue = self._draft_queue
        if queue is None:
            return DraftMatchSelection()

        # 요청 측 캐시 키는 "지금 무엇으로 생성할 것인가"의 함수다. 정체성이 없거나
        # 모델이 unverified면 ``None``이고, 그때는 generated 갈래를 아예 열지 않는다.
        request_cache_key = (
            self._generator_identity.cache_key_for(request.user_prompt_ko)
            if self._generator_identity is not None
            else None
        )
        matches = [
            (draft, basis)
            for draft in queue.list_drafts()
            if (
                basis := _match_basis(
                    draft, request=request, request_cache_key=request_cache_key
                )
            )
            is not None
        ]

        rejected_excluded = 0
        ambiguous_excluded = 0
        ranked: list[
            tuple[
                int, int, str, DraftAssetEntry, DraftMatchBasis, DraftReviewProjection
            ]
        ] = []
        for draft, basis in matches:
            try:
                projection = _review_projection(
                    queue.load_reviews(draft.draft_asset_id)
                )
            except ReviewOrderingError:
                # 이 draft의 리뷰 최신성을 판정할 수 없다. **후보 탐색은 여러 draft를
                # 훑으므로 여기서 멈추면 안 된다** — 모호한 draft 하나 때문에 무관한
                # 정상 후보까지 막히고, 요청 전체가 500이 된다. 판정 불가는 "재사용
                # 가능"이 아니므로 후보에서 빼되(fail-closed), 카운터로 드러내
                # 운영자가 손상 레코드의 존재를 알 수 있게 한다.
                #
                # 대상 draft **자신의** 권한 경계(등록 게이트·fallback 실행)는 다르다 —
                # 거긴 모호하면 멈추는 것이 옳고, 그대로 둔다.
                ambiguous_excluded += 1
                continue
            if projection is DraftReviewProjection.REJECTED:
                # 사람이 거부한 형상은 재사용 대상이 아니다. 정렬로 뒤로 밀어두는 것이
                # 아니라 **후보에서 제거**한다 — 뒤로 밀면 다른 후보가 없을 때 뽑힌다.
                rejected_excluded += 1
                continue
            ranked.append(
                (
                    _REVIEW_PROJECTION_RANK[projection],
                    _MATCH_BASIS_RANK[basis],
                    # ⚠️ 사전순은 **안정성 전용 tiebreak**이다. artifact-addressed id는
                    # SHA 계열이라 "더 나은 artifact"도 "더 최신"도 의미하지 않는다.
                    # 최신성으로 정렬할 수는 없다 — ``DraftAssetEntry``에 생성 시각
                    # 필드가 없다(``reviewed_at``은 리뷰 기록에만 있다). 없는 것으로
                    # 정렬하지 않는다.
                    draft.draft_asset_id,
                    draft,
                    basis,
                    projection,
                )
            )

        if not ranked:
            return DraftMatchSelection(
                candidate_count=len(matches),
                rejected_excluded_count=rejected_excluded,
                ambiguous_excluded_count=ambiguous_excluded,
            )

        _, _, _, draft, basis, projection = min(ranked, key=lambda row: row[:3])
        return DraftMatchSelection(
            selected=draft,
            match_basis=basis,
            review_projection=projection,
            candidate_count=len(matches),
            rejected_excluded_count=rejected_excluded,
            ambiguous_excluded_count=ambiguous_excluded,
        )

    def _matching_references(
        self,
        request: AssetIntakeRequest,
    ) -> tuple[ReferenceAssetCandidate, ...]:
        return tuple(
            candidate
            for candidate in self._reference_candidates
            if _same_identity(
                subject=candidate.subject,
                category=candidate.category,
                style=candidate.style,
                request=request,
            )
        )


def _match_basis(
    draft: DraftAssetEntry,
    *,
    request: AssetIntakeRequest,
    request_cache_key: str | None,
) -> DraftMatchBasis | None:
    """이 draft가 **후보에 드는가**, 든다면 무엇으로 맞았는가 (E2).

    generated draft가 메타데이터로는 절대 맞지 않는 이유: ``subject``/``category``/
    ``style``은 모델 호출이 **끝난 뒤** draft에 붙는 메타데이터라 캐시 키에서 일부러
    빠져 있다. 그것으로 generated draft를 재사용하면 캐시 키는 여전히 아무도 쓰지 않는
    것이고, 무엇보다 **다른 모델이 만든 형상**이 같은 subject라는 이유로 돌아온다.

    ``request_cache_key is None``을 먼저 막는 것이 이 함수의 핵심이다. legacy
    provenance draft와 unverified 모델 draft는 ``scad_cache_key is None``인데, 그
    가드가 없으면 ``None == None``으로 **전부 매칭**된다. 별도 차단 로직 없이 자동
    배제되는 것이 캐시 슬라이스가 예고한 fail-closed 설계의 회수다.
    """

    if draft.generated_openscad_source is not None:
        if request_cache_key is None or draft.scad_cache_key != request_cache_key:
            return None
        return DraftMatchBasis.SCAD_CACHE_KEY
    if _same_identity(
        subject=draft.subject,
        category=draft.category,
        style=draft.style,
        request=request,
    ):
        return DraftMatchBasis.SPEC_IDENTITY
    return None


def _review_projection(
    records: tuple[DraftAssetReviewRecord, ...],
) -> DraftReviewProjection:
    """최신 리뷰 판정을 재사용 상태로 투영한다.

    최신 리뷰 선정은 :func:`current_review`(정본)에 위임한다 — 여기서 ``max(...)``를
    다시 쓰면 "등록이 보는 최신 리뷰"와 "재사용이 보는 최신 리뷰"가 갈라진다.
    """

    latest = current_review(records)
    if latest is None:
        return DraftReviewProjection.UNREVIEWED
    return _DECISION_PROJECTION[latest.decision]


def _selection_counters(selection: DraftMatchSelection) -> dict[str, object]:
    """후보 수·배제 수 — **선택 성공/실패와 무관하게** 실리는 관측값.

    별도 helper인 이유: 이 셋이 실리는 곳이 셋(재사용 성공·유사 후보·새 초안 허용)이고,
    한 곳이라도 빠지면 그 경로에서 배제가 **조용해진다**. 실제로 첫 구현이 성공 경로에만
    실어서, 모호한 후보만 있을 때 배제가 흔적 없이 사라졌다.

    ``candidate_count > 0`` 인데 결과가 ``NEW_DRAFT_ALLOWED``면 = **후보가 있었는데 전부
    배제됐다**는 뜻이고, 그 조합만으로 운영자가 큐에 손볼 것이 있음을 알 수 있다.
    새 status를 만들지 않고 카운터로 드러내는 이유는 계약 확장을 피하기 위해서다.
    """

    return {
        "candidate_count": selection.candidate_count,
        "rejected_excluded_count": selection.rejected_excluded_count,
        "ambiguous_excluded_count": selection.ambiguous_excluded_count,
    }


def _draft_match_metadata(
    selection: DraftMatchSelection, draft: DraftAssetEntry
) -> dict[str, object]:
    """재사용 판단의 **근거**를 관측 가능하게 남긴다 (E5).

    운영자·사용자가 "왜 이 draft를 줬는지" 알 수 있어야 한다. 기존
    ``subject``/``category``/``style``/``asset_lifecycle_status`` 키는 소비자가 있으므로
    유지한다.

    ⚠️ ``generated_draft_provenance_status``는 **generated 전용 전조건**이 있어 spec
    draft에 부르면 fail-fast한다(spec도 provenance 다섯 필드가 전부 ``None``이라
    source를 보지 않는 판정식은 spec을 legacy generated로 조용히 오분류하기 때문이다).
    그래서 source가 없으면 helper를 부르지 않고 ``None``을 싣는다.
    """

    return {
        "subject": draft.subject,
        "category": draft.category,
        "style": draft.style,
        "asset_lifecycle_status": draft.asset_lifecycle_status.value,
        "match_basis": selection.match_basis.value
        if selection.match_basis is not None
        else None,
        "review_projection": selection.review_projection.value
        if selection.review_projection is not None
        else None,
        "scad_cache_key": draft.scad_cache_key,
        "provenance_status": (
            generated_draft_provenance_status(draft)
            if draft.generated_openscad_source is not None
            else None
        ),
        **_selection_counters(selection),
    }


def _same_identity(
    *,
    subject: str,
    category: str,
    style: str | None,
    request: AssetIntakeRequest,
) -> bool:
    if request.subject is not None and subject != request.subject:
        return False
    if request.category is not None and category != request.category:
        return False
    if request.subject is None or request.category is None:
        return False
    if request.style is None:
        return True
    return style == request.style


def _draft_queue_explanation(draft: DraftAssetEntry) -> AssetCandidateExplanation:
    return AssetCandidateExplanation(
        candidate_id=draft.draft_asset_id,
        asset_id=None,
        source=AssetCandidateExplanationSource.DRAFT_QUEUE,
        match_level=AssetCandidateMatchLevel.NOT_APPLICABLE,
        subject=draft.subject,
        category=draft.category,
        style=draft.style,
        reason_code=AssetIntakeReason.EXISTING_DRAFT_MATCH.value,
        display_text_ko="이미 작성 중인 초안이 있어 새 초안을 만들지 않습니다.",
    )


def _reference_candidate_explanations(
    candidates: tuple[ReferenceAssetCandidate, ...],
) -> tuple[AssetCandidateExplanation, ...]:
    return tuple(
        AssetCandidateExplanation(
            candidate_id=candidate.reference_id,
            asset_id=None,
            source=AssetCandidateExplanationSource.REFERENCE_CANDIDATE,
            match_level=AssetCandidateMatchLevel.NOT_APPLICABLE,
            subject=candidate.subject,
            category=candidate.category,
            style=candidate.style,
            reason_code=AssetIntakeReason.SIMILAR_REFERENCE_CANDIDATE.value,
            display_text_ko="유사한 참고 후보가 있어 새 에셋 생성 전에 검토가 필요합니다.",
        )
        for candidate in candidates
    )


def _new_draft_explanation(request: AssetIntakeRequest) -> AssetCandidateExplanation:
    return AssetCandidateExplanation(
        candidate_id=_new_draft_candidate_id(request),
        asset_id=None,
        source=AssetCandidateExplanationSource.NEW_DRAFT,
        match_level=AssetCandidateMatchLevel.NOT_APPLICABLE,
        subject=request.subject,
        category=request.category,
        style=request.style,
        reason_code=AssetIntakeReason.NO_EXISTING_ASSET_OR_DRAFT.value,
        display_text_ko=(
            "기존 에셋, 초안, 참고 후보가 없어 초안 생성 경계로 진행할 수 있습니다. "
            "검토 전에는 제품 후보가 아닙니다."
        ),
    )


def _new_draft_candidate_id(request: AssetIntakeRequest) -> str:
    if request.request_id is not None:
        return request.request_id
    parts = [request.subject, request.category, request.style]
    candidate_id = "_".join(part for part in parts if part)
    if candidate_id:
        return f"{candidate_id}_new_draft"
    digest = hashlib.sha256(request.user_prompt_ko.encode("utf-8")).hexdigest()[:12]
    return f"request_{digest}"


def _safe_public_id(value: str) -> str:
    if not _SAFE_PUBLIC_ID_RE.fullmatch(value):
        raise AssetIntakeError(f"Invalid public-safe asset id: {value!r}")
    return value


__all__ = [
    "AssetFolderPolicy",
    "AssetIntakeError",
    "AssetIntakeReason",
    "AssetIntakeRequest",
    "AssetIntakeResolver",
    "AssetIntakeResult",
    "AssetIntakeStatus",
    "DraftMatchBasis",
    "DraftMatchSelection",
    "DraftReviewProjection",
    "ReferenceAssetCandidate",
]
