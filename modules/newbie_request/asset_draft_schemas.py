from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.asset_catalog import (
    DecorativeAssetLegalReviewStatus,
    DecorativeAssetPrintabilityStatus,
    DecorativeAssetSourceType,
)
from modules.newbie_request.schemas import NewbieRoute, SizeMM, VisualQualityStatus


def generated_source_sha256(source: str) -> str:
    """``generated_openscad_source``의 **정본** 지문.

    이 함수가 정본인 이유: 같은 digest를 사람 리뷰 기록(``record_draft_review``),
    검토 콘솔, 실행 경로(``apps.orchestrator.fallback_draft_execution``)가 **모두**
    계산한다. 계산식이 두 벌이 되면 "사람이 본 source == 실행 source"라는 비교가
    영원히 불일치하거나(막혀야 할 게 안 막히는 대신 다 막힘) 조용히 갈라진다.

    ``modules`` 계층에 두는 이유: 소비자 중 하나가 ``apps``에 있는데 ``modules``가
    ``apps``를 import하면 계층 역의존이다. 정본은 아래(스키마)에 두고 앱은 alias만
    갖는다.

    출력은 항상 **소문자 hex 64자**다. ``DraftAssetReviewRecord`` 필드가 같은 형태만
    허용하므로 대소문자 표기 차이로 인한 거짓 불일치가 생길 수 없다.
    """

    return hashlib.sha256(source.encode("utf-8")).hexdigest()


#: provider가 모델 식별자를 노출하지 않을 때 provenance에 기록되는 값.
#:
#: 정본을 **스키마 계층**에 두는 이유: 이 값은 두 곳에서 판정된다 —
#: ``CapableModelDraftGenerator``가 기록할 때, 그리고 ``DraftAssetEntry``
#: validator가 "이 draft는 캐시 키를 가질 수 없다"를 강제할 때. 문자열이 두 벌이면
#: 어느 날 한쪽만 바뀌어 **키가 없어야 할 draft에 키가 생긴다**(= 재현 가능하다는
#: 거짓 주장). ``asset_authoring``이 이 상수를 다시 export하므로 기존 공개명
#: (``CapableModelDraftGenerator.unverified_model``)은 그대로 유지된다.
UNVERIFIED_MODEL: Final = "unverified"

#: :func:`derive_scad_cache_key` 키 구성 규칙의 버전.
#:
#: 키 입력 구성이 바뀌면 이 상수를 올린다. 그래야 **옛 규칙으로 계산된 키와 새 규칙의
#: 키가 같은 이름공간에서 섞이지 않는다** — 섞이면 규칙이 바뀐 사실이 조용히 지워지고
#: 서로 다른 job identity가 같은 키를 공유할 수 있다.
SCAD_CACHE_KEY_VERSION: Final = 1


def derive_scad_cache_key(
    *,
    originating_prompt: str,
    scad_dsl: str,
    draft_generator: str,
    draft_renderer_version: str,
    generator_provider: str,
    generator_model: str,
) -> str | None:
    """**SCAD 생성 job 정체성**의 정본 캐시 키 (fallback ADR Item 5).

    이 키가 답하는 질문은 **"같은 SCAD 생성 job인가"**이지 "같은 draft 레코드인가"가
    아니다. 그래서 ``subject``/``category``/``style``/``review_queue_reason``은 키에
    들어가지 않는다 — 모델에 실제로 전달되는 것은 프롬프트와 DSL뿐이고 그 넷은 모델
    호출이 **끝난 뒤** draft에 붙는 메타데이터다. 키에 넣으면 ``review_queue_reason``만
    바뀌어도 같은 job이 다른 키를 받아 **거짓 miss**가 된다.

    생성 **결과**(``generated_openscad_source``)도 키에 넣지 않는다. 키는 job identity
    (입력)이고 결과물 정체성은 :func:`generated_source_sha256`이 이미 소유한다. 결과를
    넣으면 "같은 job인가"를 물을 수 없어져 캐시 키의 목적 자체가 사라진다.

    **무효화 규칙에 별도 로직은 없고, 있어서도 안 된다.** ``generator_provider``·
    ``generator_model``·``draft_renderer_version``이 **키 입력**이므로 모델이 바뀌면
    키가 저절로 바뀐다. 이것이 fallback ADR의 "invalidate on model change"의 기계적 충족
    이다. 나중에 "무효화 로직이 없네"라며 별도 코드를 추가하지 말 것 — 추가하는 순간
    판정이 두 벌이 된다.

    프롬프트는 **정규화하지 않는다**(trim/lowercase/공백접기 전부 금지). 정규화는
    "서로 다른 요청을 같다고 판정"하는 위험을 만드는데, 이 슬라이스는 재사용을 켜지
    않으므로 그 위험을 감수할 이득이 없다. 필요해지면 재사용 배선이 **명시적 결정**
    으로 도입한다.

    Returns:
        모델이 :data:`UNVERIFIED_MODEL`이면 ``None``. provider가 모델을 노출하지
        않으면 "모델이 바뀌었는가"를 **기계적으로 판정할 수 없고**, ``"unverified"``를
        키 입력에 그대로 넣으면 모델이 바뀌어도 키가 같아서 **재현 가능하다는 거짓
        주장**을 하게 된다. 키가 없는 것이 "재현 가능하다고 주장하지 않음"의 정직한
        표현이다(fail-closed). 그 외에는 canonical JSON(``sort_keys``·``ensure_ascii``
        off·최소 구분자) → UTF-8 → SHA-256의 소문자 hex 64자.
    """

    if generator_model == UNVERIFIED_MODEL:
        return None
    payload: dict[str, object] = {
        "cache_key_version": SCAD_CACHE_KEY_VERSION,
        "originating_prompt": originating_prompt,
        "scad_dsl": scad_dsl,
        "draft_generator": draft_generator,
        "draft_renderer_version": draft_renderer_version,
        "generator_provider": generator_provider,
        "generator_model": generator_model,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ScadGeneratorIdentity(BaseModel):
    """ "지금 무엇으로 SCAD를 생성할 것인가"의 **정체성 snapshot** (fallback ADR Item 5).

    존재 이유: intake는 생성 **전에** 일어나므로, 요청 측에서
    :func:`derive_scad_cache_key`를 부르려면 아직 만들지 않은 생성물의 생성기
    정체성을 알아야 한다. 그렇다고 생성기 인스턴스 자체를 intake에 주입하면
    intake가 LLM 호출 능력까지 갖게 되어 경계가 번진다 — 그래서 **호출 능력이 없는
    값**만 넘긴다.

    ⚠️ **이 값을 소비자가 재선언하지 말 것.** 정본은 실제 생성기
    (``CapableModelDraftGenerator.scad_identity``)이고, 소비자는 그것을 받아쓰기만
    한다. intake가 ``"openscad"``를 하드코딩하거나 생성기 이름을 다시 적으면 정체성
    정의가 두 벌이 되어, DSL이나 renderer 버전이 바뀌는 날 **생성은 새 키로 저장하는데
    조회는 옛 키로 물어보는** 거짓 miss가 된다.

    :meth:`cache_key_for`가 여기 있는 이유도 같다 — 키 계산식 호출부가 생성 측과 조회
    측 두 곳에 흩어지면 인자 하나가 갈라져도 아무도 모른다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_generator: str = Field(min_length=1)
    draft_renderer_version: str = Field(min_length=1)
    generator_provider: str = Field(min_length=1)
    generator_model: str = Field(min_length=1)
    scad_dsl: Literal["openscad"]

    def cache_key_for(self, originating_prompt: str) -> str | None:
        """이 정체성으로 ``originating_prompt``를 생성할 job의 캐시 키.

        ``None``이면 **키를 주장할 수 없다**는 뜻이다(모델 미확인). 그 경우 조회 측은
        generated draft 재사용을 하지 않아야 한다 — ``None == None``으로 맞추면
        "모델을 모르는 것끼리 같은 job"이라고 판정하는 것이라 fail-closed 설계를
        정확히 뒤집는다.
        """

        return derive_scad_cache_key(
            originating_prompt=originating_prompt,
            scad_dsl=self.scad_dsl,
            draft_generator=self.draft_generator,
            draft_renderer_version=self.draft_renderer_version,
            generator_provider=self.generator_provider,
            generator_model=self.generator_model,
        )


class AssetLifecycleStatus(StrEnum):
    DRAFT = "draft"
    REVIEW_CANDIDATE = "review_candidate"
    REJECTED = "rejected"
    ACCEPTED_FOR_REGISTRATION = "accepted_for_registration"


class DraftReviewDecision(StrEnum):
    REJECT = "reject"
    CANDIDATE = "candidate"
    ACCEPT = "accept"


class DraftAssetEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    asset_lifecycle_status: AssetLifecycleStatus = AssetLifecycleStatus.DRAFT
    subject: str = Field(min_length=1)
    category: str = Field(min_length=1)
    style: str = Field(min_length=1)
    source_type: DecorativeAssetSourceType
    license: str = Field(min_length=1)
    source_url_or_owner: str = Field(min_length=1)
    provenance_note: str = Field(min_length=1)
    originating_prompt: str = Field(min_length=1)
    draft_generator: str = Field(min_length=1)
    draft_renderer_version: str = Field(min_length=1)
    review_queue_reason: str = Field(min_length=1)
    generated_openscad_source: str | None = Field(default=None, min_length=1)
    #: 아래 다섯 필드가 **구조화 provenance**다 (fallback ADR Item 5).
    #:
    #: ``provenance_note`` prose는 그대로 남지만 그건 사람이 읽는 감사 **서술**이고,
    #: 기계 판정의 정본은 이 필드들이다. **prose를 파싱하지 말 것** — 같은 사실이 두
    #: 곳에 있게 되지만, 한쪽은 서술이고 한쪽은 계약이다.
    #:
    #: 전부 generated draft 전용이다. SCAD source가 없는 spec draft는 SCAD provenance도
    #: 있을 수 없으므로 다섯 필드가 전부 ``None``이어야 한다(아래 validator가 강제).
    generator_provider: str | None = Field(default=None, min_length=1)
    generator_model: str | None = Field(default=None, min_length=1)
    #: 오늘은 ``"openscad"`` 하나뿐이지만 **상수가 아니라 필드**인 이유: (i) validator가
    #: 캐시 키를 재계산하려면 키 입력을 draft 자신이 들고 있어야 하고, (ii) 두 번째 DSL이
    #: 생겼을 때 **기존 draft가 어떤 DSL로 키를 계산했는지 복원**할 수 있어야 한다.
    scad_dsl: Literal["openscad"] | None = None
    #: ``"cached"``를 지금 열지 않는 이유: 이 슬라이스에는 cached가 나올 수 있는 경로가
    #: 없다. ``str``로 열어두면 조용히 새어나오므로 ``Literal``로 못박고, 재사용 배선
    #: 슬라이스가 **명시적으로** 확장하게 한다.
    scad_origin: Literal["generated"] | None = None
    #: :func:`derive_scad_cache_key`가 유도한 값. 호출자가 임의로 **주입할 수 없다** —
    #: validator가 직접 재계산해 대조하므로 유효 형식의 아무 hex나 심는 것이 거부된다.
    scad_cache_key: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    default_size_mm: SizeMM | None = None
    min_size_mm: SizeMM | None = None
    recommended_thickness_mm: float | None = Field(default=None, gt=0)
    required_features: tuple[str, ...] | None = Field(default=None, min_length=1)
    visual_quality_status: VisualQualityStatus = VisualQualityStatus.REVIEW_REQUIRED
    legal_review_status: DecorativeAssetLegalReviewStatus = (
        DecorativeAssetLegalReviewStatus.NOT_REVIEWED
    )
    printability_status: DecorativeAssetPrintabilityStatus = (
        DecorativeAssetPrintabilityStatus.REVIEW_REQUIRED
    )
    release_allowed: bool = False
    runtime_catalog_registered: bool = False
    reviewer: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None

    @model_validator(mode="after")
    def _draft_starts_closed(self) -> DraftAssetEntry:
        if self.asset_lifecycle_status is not AssetLifecycleStatus.DRAFT:
            raise ValueError("new draft assets must start with lifecycle status draft")
        if self.visual_quality_status is not VisualQualityStatus.REVIEW_REQUIRED:
            raise ValueError("draft assets must start with visual_quality_status=review_required")
        if self.legal_review_status is not DecorativeAssetLegalReviewStatus.NOT_REVIEWED:
            raise ValueError("draft assets must start with legal_review_status=not_reviewed")
        if self.printability_status is not DecorativeAssetPrintabilityStatus.REVIEW_REQUIRED:
            raise ValueError("draft assets must start with printability_status=review_required")
        if self.release_allowed:
            raise ValueError("draft assets must start with release_allowed=false")
        if self.runtime_catalog_registered:
            raise ValueError("draft assets must not be runtime catalog registered")
        return self

    @model_validator(mode="after")
    def _spec_and_generated_are_exclusive(self) -> DraftAssetEntry:
        spec_fields = (
            self.default_size_mm,
            self.min_size_mm,
            self.recommended_thickness_mm,
            self.required_features,
        )
        if self.generated_openscad_source is None:
            if any(field is None for field in spec_fields):
                raise ValueError(
                    "deterministic spec drafts must populate all of "
                    "default_size_mm, min_size_mm, recommended_thickness_mm, "
                    "and required_features when generated_openscad_source is absent"
                )
        else:
            if any(field is not None for field in spec_fields):
                raise ValueError(
                    "generated drafts carry OpenSCAD source only; "
                    "default_size_mm, min_size_mm, recommended_thickness_mm, "
                    "and required_features must all be None"
                )
        return self

    @model_validator(mode="after")
    def _scad_provenance_is_complete_or_legacy(self) -> DraftAssetEntry:
        """SCAD provenance는 **양방향** 불변식이다 (fallback ADR Item 5 D6·D8).

        "source가 없으면 provenance 금지"만 두고 역방향을 빠뜨리면, source가 있는
        draft에 provider/model 없이 임의의 64자 hex를 ``scad_cache_key``로 주입해도
        통과한다 — 그러면 이건 스키마 **계약**이 아니라 생성기의 **관례**일 뿐이다.

        generated draft에 허용되는 형상은 정확히 **둘**이고 그 사이의 어떤 부분 상태도
        거부한다. 부분 상태는 "일부는 알고 일부는 모른다"인데, provenance로서 아무것도
        보증하지 못하면서 보증하는 것처럼 보인다.

        (A) **완전 provenance** — provider·model·dsl 셋 다 있고 origin이 generated이며
            키가 :func:`derive_scad_cache_key`의 유도값과 정확히 일치한다. 키를 *주입*이
            아니라 *유도*로 만드는 것이 이 대조의 목적이다.
        (B) **legacy provenance** — 다섯 필드 전부 ``None``. 이 필드들 **이전에** 디스크에
            저장된 generated draft다. 이걸 허용하지 않으면 마이그레이션 없이는
            ``load_draft``가 깨진다. 다만 "읽을 수 있다"와 "새로 만들 수 있다"는 다르다 —
            새 legacy 생성은 :meth:`DraftReviewQueue.save_draft`의 저장 경계가 막는다.
        """

        provenance = (
            self.generator_provider,
            self.generator_model,
            self.scad_dsl,
            self.scad_origin,
            self.scad_cache_key,
        )
        if self.generated_openscad_source is None:
            # spec draft: SCAD source가 없으니 SCAD provenance도 있을 수 없다.
            if any(field is not None for field in provenance):
                raise ValueError(
                    "spec drafts carry no SCAD provenance; generator_provider, "
                    "generator_model, scad_dsl, scad_origin and scad_cache_key "
                    "must all be None when generated_openscad_source is absent"
                )
            return self

        if all(field is None for field in provenance):
            return self  # (B) legacy provenance — 읽기 전용 과거.

        # 여기부터는 (A) 완전 provenance만 허용된다. 하나라도 비면 부분 상태다.
        if self.generator_provider is None or self.generator_model is None or self.scad_dsl is None:
            raise ValueError(
                "generated drafts must carry complete SCAD provenance "
                "(generator_provider, generator_model and scad_dsl are all "
                "required together) or none of it at all (legacy records); "
                "partial provenance guarantees nothing while looking like it does"
            )
        if self.scad_origin != "generated":
            raise ValueError(
                "generated drafts with complete SCAD provenance must record "
                f"scad_origin='generated', got {self.scad_origin!r}"
            )
        expected_cache_key = derive_scad_cache_key(
            originating_prompt=self.originating_prompt,
            scad_dsl=self.scad_dsl,
            draft_generator=self.draft_generator,
            draft_renderer_version=self.draft_renderer_version,
            generator_provider=self.generator_provider,
            generator_model=self.generator_model,
        )
        if self.scad_cache_key != expected_cache_key:
            if expected_cache_key is None:
                raise ValueError(
                    f"generator_model={UNVERIFIED_MODEL!r} cannot claim a "
                    "reproducible scad_cache_key: the model identity is unknown, so "
                    "'invalidate on model change' is not mechanically decidable; "
                    "scad_cache_key must be None"
                )
            raise ValueError(
                "scad_cache_key is derived, not injected: it must equal "
                f"derive_scad_cache_key(...) = {expected_cache_key!r}, got "
                f"{self.scad_cache_key!r}"
            )
        return self


def generated_draft_provenance_status(
    draft: DraftAssetEntry,
) -> Literal["complete", "legacy"]:
    """generated draft의 provenance 형상을 분류한다 — **정본 판정** (fallback ADR Item 5 D10).

    소비자가 "provider를 안 실었다"와 "provider를 알 수 없다"를 구분할 수 있어야 한다.
    manifest 빌더나 저장 경계가 자체 None-count 로직을 갖지 말고 **이 함수를 호출**하라 —
    판정이 두 벌이면 영원히 불일치한다.

    **전조건: generated draft 전용.** spec draft를 넣으면 fail-fast한다. spec draft도
    다섯 필드가 전부 ``None``인 유효한 entry라서, source를 보지 않는 판정식은 **spec을
    legacy generated로 조용히 오분류**한다. 오늘 실행 경로가 spec을 게이트에서 거부하므로
    manifest 빌더는 spec을 만나지 않지만, 그 도달 불가능성은 **실행 경로에만** 있고 스키마
    계층 helper의 계약에는 없다.

    ``"complete"``의 조건에 "키가 non-None"이 아니라 "키가 스키마 불변식을 만족"을 쓰는
    이유: :data:`UNVERIFIED_MODEL` draft는 ``scad_cache_key is None``이 **합법**이므로,
    다섯 필드의 None 개수로 세면 **정상 unverified draft가 legacy로 오분류**된다.

    Raises:
        ValueError: spec draft가 들어왔거나(전조건 위반), 스키마가 이미 막았어야 할
            도달 불가 상태일 때. 조용히 한쪽으로 분류하지 않는다.
    """

    if draft.generated_openscad_source is None:
        raise ValueError(
            f"generated_draft_provenance_status requires a generated draft; "
            f"{draft.draft_asset_id!r} carries no generated_openscad_source. A spec "
            "draft has the same five None fields as a legacy generated draft and "
            "would be misclassified as 'legacy'."
        )
    provenance = (
        draft.generator_provider,
        draft.generator_model,
        draft.scad_dsl,
        draft.scad_origin,
        draft.scad_cache_key,
    )
    if all(field is None for field in provenance):
        return "legacy"
    if (
        draft.generator_provider is not None
        and draft.generator_model is not None
        and draft.scad_dsl is not None
        and draft.scad_origin == "generated"
        # 키의 **값** 유효성은 ``_scad_provenance_is_complete_or_legacy``가 이미
        # 재계산으로 강제한다. 여기서 다시 계산하면 판정이 두 벌이 된다. 남은 축은
        # unverified 모델이 키를 갖지 않는다는 형상 조건뿐이다.
        and (draft.scad_cache_key is None) == (draft.generator_model == UNVERIFIED_MODEL)
    ):
        return "complete"
    raise ValueError(
        f"draft {draft.draft_asset_id!r} is in a SCAD provenance state the schema "
        "should have rejected (neither complete nor legacy); refusing to classify it "
        "as either"
    )


class DraftAssetAuthoringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_prompt_ko: str = Field(min_length=1)
    subject: str | None = None
    category: str | None = None
    style: str | None = None
    review_queue_reason: str = "no_verified_runtime_catalog_asset"


class DraftAssetAuthoringStatus(StrEnum):
    DRAFT_CREATED = "draft_created"
    DRAFT_REUSE_AVAILABLE = "draft_reuse_available"
    SIMILAR_REFERENCE_AVAILABLE = "similar_reference_available"
    ASK_USER = "ask_user"
    RUNTIME_CATALOG_AVAILABLE = "runtime_catalog_available"


class DraftAssetAuthoringResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DraftAssetAuthoringStatus
    reason: str
    draft_asset: DraftAssetEntry | None = None
    runtime_asset_id: str | None = None
    intake_status: str | None = None
    intake_reason: str | None = None
    intake_new_draft_allowed: bool | None = None
    intake_metadata: dict[str, object] = Field(default_factory=dict)
    candidate_asset_ids: tuple[str, ...] = ()
    reference_candidate_ids: tuple[str, ...] = ()
    selected_route: NewbieRoute


class DraftAssetReviewRecord(BaseModel):
    """사람이 내린 검토 판단의 durable 기록.

    ``reviewed_source_sha256``이 이 기록의 **대상**을 못박는다: 그것이 없으면
    "무엇을 보고 판단했는가"가 기록에 없어서, source A를 ACCEPT한 뒤 같은 draft id의
    파일을 B로 바꾸고 B를 새로 게이트하면 모든 비교가 통과하는데 사람은 B를 본 적이
    없게 된다. 값은 팩토리(``record_draft_review``)가 draft에서 **스스로** 계산하며
    호출자가 주입할 수 없다 — 주입 가능하면 "리뷰 대상이 아닌 digest"를 넣는 순간
    결속이 무의미해진다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    decision: DraftReviewDecision
    asset_lifecycle_status: AssetLifecycleStatus
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    review_note: str = Field(min_length=1)
    required_next_gates: tuple[str, ...]
    #: 검토된 ``generated_openscad_source``의 SHA-256(:func:`generated_source_sha256`).
    #: spec 기반 draft는 결속할 source가 없으므로 ``None``이 정상이다. 기본값이
    #: ``None``인 것은 **하위호환**을 위해서다 — 이 필드 이전에 기록된 리뷰 JSON도
    #: 그대로 로드돼야 한다. 다만 "로드된다"와 "실행이 허용된다"는 다르다: 실행
    #: 경로는 generated-source draft의 ``None`` ACCEPT를 fail-closed로 거부한다.
    #: 소문자 hex만 허용해 표기 차이로 인한 거짓 불일치를 표현 불가능하게 만든다.
    reviewed_source_sha256: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reviewed_artifact_sha256: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reviewed_prepare_id: str | None = Field(default=None, pattern=r"^prepare_[0-9a-f]{32}$")
    visual_quality_status: VisualQualityStatus | None = None
    legal_review_status: DecorativeAssetLegalReviewStatus | None = None
    commercial_allowed: bool | None = None
    redistribution_allowed: bool | None = None
    modification_allowed: bool | None = None
    release_allowed: bool = False
    runtime_catalog_registered: bool = False

    @model_validator(mode="after")
    def _review_record_does_not_release(self) -> DraftAssetReviewRecord:
        if self.release_allowed:
            raise ValueError("draft review records must not grant release")
        if self.runtime_catalog_registered:
            raise ValueError("draft review records must not auto-register runtime catalog")
        return self


class DraftAssetPrepareRecord(BaseModel):
    """게이트가 본 source와 사람이 볼 산출물을 묶는 불변 prepare 정본."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prepare_id: str = Field(pattern=r"^prepare_[0-9a-f]{32}$")
    draft_asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    source_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    stl_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    stl_path: str = Field(min_length=1)
    preview_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    preview_path: str = Field(min_length=1)
    gate_verdict: dict[str, object]
    cad_adapter: str = Field(min_length=1)
    cad_adapter_version: str = Field(min_length=1)
    prepared_at: str = Field(min_length=1)


class DraftAssetEvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    evidence_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    evidence_type: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[A-Fa-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    metadata: dict[str, object] = Field(default_factory=dict)


#: The only pre-filter verdict that may be routed to the manual-review queue
#: (fallback ADR item 6). ``blocked_prohibited`` is a *block*, not a review request:
#: putting a prohibited category into a review queue would read as "reviewable"
#: and invert the policy. Callers branch on this constant; the record schema
#: below enforces the same invariant structurally.
MANUAL_REVIEW_IP_DECISION: Final = "manual_review_ip"


class PrefilterManualReviewRecord(BaseModel):
    """A durable manual-review request produced by the IP/abuse pre-filter.

    Mirrors :class:`DraftAssetEvidenceRecord` (frozen, ``extra="forbid"``, same
    id pattern). It deliberately does NOT reuse the draft/review records: a
    pre-filter block happens before any draft exists, so there is no
    ``draft_asset_id`` to hang it on.

    ``decision`` is a one-value ``Literal`` on purpose — the policy "only
    ``manual_review_ip`` reaches the review queue" is then enforced by the
    schema itself, so a ``blocked_prohibited`` record cannot even be
    constructed, let alone stored.

    ``subject``/``category`` are intentionally absent: at block time they are
    unverified router guesses, and the record keeps the minimum a human needs
    to see what was blocked and why.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    created_at: str = Field(min_length=1)
    originating_prompt: str = Field(min_length=1)
    decision: Literal["manual_review_ip"]
    # Nullable because ``FallbackGateDecision.matched_rule_id`` is, but still
    # required: an omitted rule id would quietly look like "no rule matched".
    matched_rule_id: str | None
    reason_code: str = Field(min_length=1)
    user_message_ko: str = Field(min_length=1)


class PrefilterManualReviewDisposition(BaseModel):
    """차단된 요청에 사람이 내린 **처분**의 durable 감사 기록 (fallback ADR item 6).

    ``PrefilterManualReviewRecord``가 "무엇이 왜 차단됐나"라면 이 레코드는
    "사람이 그 차단을 어떻게 판단했나"다. 둘을 한 레코드로 합치지 않는 이유:
    차단 기록은 사전필터가 즉시 쓰고 처분은 나중에 사람이 쓰므로, 합치면
    감사 기록을 **덮어써야만** 처분을 남길 수 있다.

    어휘가 ``approved``/``rejected``가 아닌 이유: "승인"은 *무엇을* 승인했는지
    모호하다(요청을 승인? 차단을 승인?). 사람이 실제로 하는 일은 차단을
    **유지**(``block_upheld``)하거나 **번복**(``block_overturned``)하는 것이고,
    감사 기록에서 그 모호함은 치명적이다.

    ⚠️ **``block_overturned``는 기록이지 우회가 아니다.** 이 레코드는 사전필터를
    무력화하거나 재생성을 촉발하지 않는다. 결정론 정책 가드를 감사 기록이 조용히
    allowlist로 바꾸면 한 번의 번복이 프롬프트 텍스트를 키로 하는 **만료도 범위도
    없는 영구 우회**가 된다. 집행이 필요하면 범위·만료·권한을 갖춘 별도 정책
    설계가 선행돼야 한다.

    원문(``originating_prompt``·``reason_code``·``matched_rule_id``)은 링크된
    ``PrefilterManualReviewRecord``에 이미 있으므로 복제하지 않는다 — 두 벌이
    되면 갈라진다. 처분은 ``review_id``로 가리키기만 한다.

    ``review_id``의 pattern은 ``PrefilterManualReviewRecord``와 **동일**하다.
    다르면 스키마 생성은 되는데 큐 저장이 ``DraftQueueError``로 죽는 갈라짐이
    생긴다(큐가 같은 패턴만 경로로 허용한다).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    decision: Literal["block_upheld", "block_overturned"]
    reviewer: str = Field(min_length=1)
    decided_at: str = Field(min_length=1)
    #: 근거 없는 IP/상표 판정은 감사 불가다 — 빈 문자열을 스키마가 거부한다.
    rationale: str = Field(min_length=1)


__all__ = [
    "MANUAL_REVIEW_IP_DECISION",
    "SCAD_CACHE_KEY_VERSION",
    "UNVERIFIED_MODEL",
    "AssetLifecycleStatus",
    "DraftAssetAuthoringRequest",
    "DraftAssetAuthoringResult",
    "DraftAssetAuthoringStatus",
    "DraftAssetEntry",
    "DraftAssetEvidenceRecord",
    "DraftAssetReviewRecord",
    "DraftReviewDecision",
    "PrefilterManualReviewDisposition",
    "PrefilterManualReviewRecord",
    "ScadGeneratorIdentity",
    "derive_scad_cache_key",
    "generated_draft_provenance_status",
    "generated_source_sha256",
]
