"""승인된 capable-model fallback draft를 실제 제조 런타임으로 실행한다.

fallback ADR Item 3. `runtime_asset_execution`은 **curated 카탈로그 asset**을
렌더러로 SCAD를 만들어 실행하는 경로다. capable-model draft는 렌더러가 없고
`generated_openscad_source`(원시 SCAD 문자열)를 들고 있어서, 그 경로로는 실행될
수 없었다 — `promote_fallback_draft`는 **판정만** 반환하고 등록·실행을 하지
않는다. 이 모듈이 그 빈 칸을 메운다.

이 경로의 신규성은 **입력이 렌더러가 아니라 사람이 승인한 draft라는 점**뿐이다.
`cad.generate → L2 → printability → slice → real-Orca 검사`는 curated 경로와
**같은 공통 helper**(`runtime_asset_execution`)를 호출한다 — 복제하면 한쪽만
고쳐지는 날이 온다.

무엇을 하지 **않는가**
----------------------
* **런타임 카탈로그 등록·release 승인을 하지 않는다.** 산출 manifest는 항상
  ``runtime_catalog_registered=False``·``release_allowed=False``다. 이 경로가
  증명하는 것은 "승인된 draft가 실제로 실행돼 real Orca G-code가 나온다"이며,
  fallback ADR 3번에 따라 fallback 산출물은 **결코 verified asset이 아니다**.
* **게이트·registration 판정 로직을 새로 만들지 않는다.** 자동 게이트 판정은
  `FallbackVerificationGate`, 등록 후보 판정은 `evaluate_registration_gate`가
  소유하고 이 모듈은 **호출만** 한다.
* **미적/기능 품질을 주장하지 않는다.** 사람 ACCEPT는 "등록 후보로 받아들임"이고
  자동 게이트는 구조적으로 기능 정합성을 증명할 수 없다
  (``functional_correctness_certified`` 는 ``Literal[False]``).

전제조건이 2단계인 이유 (실측된 순서 의존성)
-------------------------------------------
`evaluate_registration_gate`가 ``CANDIDATE``를 내려면 ``SMOKE_PASSED``인
**printability report가 필수**인데, 그 report는 STL이 있어야 만들어지고 STL은
``cad.generate`` 이후에만 생긴다. 따라서 "등록 후보가 아니면 CAD 0회"는 성립
불가능한 계약이다. 대신:

* **[CAD 전]** source 정본 확정 + SHA-256 동일성 · 자동 게이트 PASS · 최신
  사람 리뷰 ACCEPT · **그 리뷰가 결속한 source == 실행 source** · sandbox 토큰
  재검사 → 하나라도 깨지면 ``cad.generate`` **0회**.
* **[CAD 후 · slice 전]** 실제 L2/printability report로 등록 후보 판정 →
  ``CANDIDATE``가 아니면 ``slicer.slice`` **0회**.

가짜/선제 printability report를 만들어 후단을 앞당기지 않는다. 그것이 이 설계의
핵심 금지사항이다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

from apps.orchestrator.dependencies import Dependencies
from apps.orchestrator.exceptions import SessionStateError
from apps.orchestrator.runtime_asset_execution import (
    generate_manufacturing_mesh,
    slice_manufacturing_mesh,
)
from apps.orchestrator.schemas import SessionEventKind
from apps.orchestrator.sessions import Session, make_event
from modules.artifacts import (
    ArtifactKind,
    ArtifactRef,
    ManifestStatus,
    SubtaskArtifactManifest,
    build_artifact_ref,
    get_artifact_root,
    write_failure_manifest,
    write_manifest,
)
from modules.cad_mechanical.adapters.mock import MockMechanicalCADGenerator

# 게이트가 쓰는 것과 **같은** OpenSCAD external-file 정규식을 재사용한다. 새
# 정규식을 쓰면 두 벌이 되어 갈라지고, 보안 판정이 갈라지는 것은 곧 우회다.
# (`fallback_verification_gate`와 `asset_authoring`도 같은 상수를 직접 쓴다 —
#  이 리포의 확립된 선례이며, 게이트 내부 private 메서드는 이번 범위에서 수정
#  대상이 아니므로 추출하지 않는다.)
from modules.cad_mechanical.adapters.openscad import _EXTERNAL_FILE_ACCESS_RE
from modules.newbie_request import (
    evaluate_assembly_stl,
    evaluate_printability,
    render_stl_preview,
    require_printability_for_slicing,
)
from modules.newbie_request.asset_catalog import (
    DecorativeAssetLegalReviewStatus,
    DecorativeAssetPrintabilityStatus,
)
from modules.newbie_request.asset_draft_schemas import (
    DraftAssetEntry,
    DraftAssetPrepareRecord,
    DraftAssetReviewRecord,
    DraftReviewDecision,
    generated_draft_provenance_status,
    generated_source_sha256,
)

# 최신 리뷰 선정 규칙(= UTC instant 최대값, 동률이면 fail-fast)을 registration
# gate·draft 재사용과 **한 벌로** 유지하기 위해 정본 helper를 그대로 재사용한다.
# 여기서 ``max(...)``를 다시 쓰면 정책이 두 벌이 되어, 어느 날 한쪽만 바뀌면
# "실행은 허용하는데 등록 판정은 거부하는" 조용한 갈라짐이 생긴다.
from modules.newbie_request.asset_registration import (
    RegistrationGateResult,
    RegistrationGateStatus,
    current_review,
)
from modules.newbie_request.draft_queue import DraftReviewQueue
from modules.newbie_request.printability import PrintabilityReport, PrintabilityStatus
from modules.newbie_request.schemas import L2GeometryReport, VisualQualityStatus
from modules.template.fallback_promotion import promote_fallback_draft
from modules.template.fallback_verification_gate import (
    FallbackGateOutcome,
    FallbackGateRequest,
    FallbackGateVerdict,
    FallbackVerificationGate,
)

#: manifest/이벤트에서 이 경로를 식별하는 stage 이름.
EXECUTION_STAGE = "fallback_draft_execution"

#: :class:`GatedFallbackDraft`를 만들 자격 토큰. :func:`gate_fallback_draft`만
#: 들고 있으며, 이걸 import해서 기록을 조립하는 코드는 **의도적 우회**다.
_GATE_BINDING_TOKEN: Final = object()


class FallbackDraftExecutionError(SessionStateError):
    """이 경로의 모든 거부의 상위 타입.

    ``SessionStateError``를 상속하는 이유: 이후 운영 표면(HTTP 등)이 붙을 때
    기존 세션-상태 위반과 같은 취급(409)을 받는 것이 옳고, 그 매핑을 두 벌로
    만들지 않기 위해서다. 하위 타입을 나누는 이유는 **어떤 전제가 깨졌는지**
    호출자와 테스트가 구분할 수 있어야 하기 때문이다 — 단일 문자열로 뭉치면
    "리뷰가 없었다"와 "파일이 바뀌었다"가 같은 사건으로 보인다.
    """


class FallbackGateNotPassedError(FallbackDraftExecutionError):
    """자동 게이트가 PASS가 아니다."""


class FallbackDraftSourceMissingError(FallbackDraftExecutionError):
    """큐의 draft가 ``generated_openscad_source``를 갖지 않는다."""


class FallbackDraftSourceMismatchError(FallbackDraftExecutionError):
    """게이트가 검증한 source와 실행 시점 큐의 source가 다르다(stale gate).

    토큰 재검사만으로 이전 게이트의 complexity·L2·printability 판정까지 계승할
    수는 없다. 그러므로 내용이 바뀌었으면 **다시 게이트를 통과해야** 한다.
    """


class FallbackDraftReviewMissingError(FallbackDraftExecutionError):
    """사람 리뷰 기록이 아예 없다."""


class FallbackDraftReviewNotAcceptedError(FallbackDraftExecutionError):
    """최신 사람 리뷰가 ACCEPT가 아니다."""

    def __init__(self, message: str, *, decision: DraftReviewDecision) -> None:
        super().__init__(message)
        self.decision = decision


class FallbackDraftReviewSourceUnboundError(FallbackDraftExecutionError):
    """최신 ACCEPT가 **어떤 source를 보고 내린 판단인지 기록하지 않았다**.

    이 필드가 생기기 전에 기록된 리뷰가 여기 해당한다. "예전 형식이니 봐준다"로
    통과시키면 이 경로가 닫으려는 갭(사람이 본 적 없는 source의 실행)이 그대로
    남으므로 fail-closed로 거부한다(R10). 해소 방법은 grandfather가 아니라
    **다시 검토**다.
    """


class FallbackDraftReviewSourceMismatchError(FallbackDraftExecutionError):
    """사람이 검토한 source와 실행할 source가 다르다.

    ``FallbackDraftSourceMismatchError``(게이트가 낡음)와 **구분되는 별도 타입**인
    이유: 운영자에게 "자동 게이트를 다시 돌려라"와 "사람이 다른 형상을 봤으니 다시
    검토받아라"는 서로 다른 조치다. 한 타입으로 뭉치면 그 구분이 사라진다.
    """


class FallbackDraftSandboxViolationError(FallbackDraftExecutionError):
    """실행 시점 source가 금지된 external-file 지시어를 담고 있다."""


class FallbackDraftNotRegistrationCandidateError(FallbackDraftExecutionError):
    """실제 report로 판정한 결과 등록 후보가 아니다 → 슬라이싱하지 않는다."""

    def __init__(self, message: str, *, result: RegistrationGateResult) -> None:
        super().__init__(message)
        self.result = result


class FallbackDraftPrepareBindingError(FallbackDraftExecutionError):
    """prepare 정본·리뷰·실행 인자가 서로 결속되지 않는다."""


class FallbackDraftPolicyJudgmentMissingError(FallbackDraftExecutionError):
    """최신 ACCEPT에 필수 사람 정책 판단이 하나 이상 누락됐다."""


class FallbackDraftPreparedArtifactMismatchError(FallbackDraftExecutionError):
    """사람이 본 준비 STL의 현재 바이트가 리뷰 digest와 다르다."""


@dataclass(frozen=True, slots=True)
class GatedFallbackDraft:
    """ "게이트가 **이 draft의 source를** 판정했다"는 사실을 함께 들고 다니는 기록.

    ``FallbackGateVerdict``는 자신이 어떤 source를 판정했는지 **기록하지 않는다**.
    그래서 verdict와 draft를 손으로 짝지어 넘길 수 있게 두면, source A로 얻은
    PASS를 source B의 draft에 붙이는 것이 API상 정상 사용과 구분되지 않는다 —
    자동 게이트의 complexity·L2·printability 판정이 **판정되지 않은 source로
    이전**되는 것이다.

    그래서 실행 API는 verdict가 아니라 이 기록을 받고, 이 기록을 만드는 지원 경로는
    :func:`gate_fallback_draft` **하나뿐**이다. 그 함수는 게이트에 넣는 source를
    ``draft.generated_openscad_source``에서 직접 꺼내므로 결속이 구성상 성립한다.

    (게이트 verdict 스키마에 source digest를 넣는 것이 근본 해법이지만 그것은
    ``modules/template`` 변경이라 이 페이즈의 범위 밖이다. 이 기록은 그 자리를
    호출자 규율이 아니라 **타입**으로 메운다.)

    ``issued_by`` 토큰을 요구하는 이유: 이 기록을 아무나 조립할 수 있으면 "지원
    경로가 하나뿐"이라는 말이 주석 수준의 약속으로 내려앉는다. 토큰을 요구하면
    우회는 **모듈 private 상수를 일부러 import하는 눈에 띄는 행위**가 되고,
    ``_GATE_BINDING_TOKEN``을 grep하는 것만으로 전수 조사할 수 있다. (같은 프로세스
    안의 악의적 호출자를 막는 장치는 아니다 — 그런 호출자는 어댑터를 직접 부르면
    된다. 이 장치가 막는 것은 **모르고 하는 오조립과 시간에 따른 표류**다.)
    """

    verdict: FallbackGateVerdict
    draft: DraftAssetEntry
    gated_source_sha256: str
    issued_by: object = None

    def __post_init__(self) -> None:
        if self.issued_by is not _GATE_BINDING_TOKEN:
            raise TypeError(
                "GatedFallbackDraft must be produced by gate_fallback_draft(); "
                "hand-assembling one detaches the gate verdict from the source it "
                "actually judged."
            )


def gate_fallback_draft(
    draft: DraftAssetEntry,
    *,
    gate: FallbackVerificationGate,
    precompiled_stl_path: Path | None = None,
) -> GatedFallbackDraft:
    """draft **자신의** source로 자동 게이트를 돌리고 그 결속을 기록한다.

    게이트 입력을 인자로 받지 않는 것이 핵심이다 — 받는 순간 "판정한 source"와
    "실행할 draft"가 갈라질 수 있다.
    """

    source = draft.generated_openscad_source
    if source is None:
        raise FallbackDraftSourceMissingError(
            f"Draft {draft.draft_asset_id!r} carries no generated OpenSCAD source; "
            "only capable-model generated drafts can be gated on this path."
        )
    outcome = gate.evaluate(
        FallbackGateRequest(scad_source=source, precompiled_stl_path=precompiled_stl_path)
    )
    return GatedFallbackDraft(
        verdict=outcome.verdict,
        draft=draft,
        gated_source_sha256=source_digest(source),
        issued_by=_GATE_BINDING_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class FallbackDraftExecutionResult:
    """실행 산출물 — 제품 승격이 아니라 **제조 런타임 증거**다.

    ``runtime_catalog_registered``/``release_allowed``를 ``Literal[False]``로
    못박는 이유: 이 경로가 등록·릴리스를 표현할 수 있으면, 어느 날 누군가
    ``True``를 넣는 것으로 fallback ADR 3번(fallback 산출물은 결코 verified asset이
    아니다)이 코드 한 줄로 뒤집힌다. 타입이 그것을 표현 불가능하게 만든다.
    """

    draft_asset_id: str
    subtask_id: str
    source_sha256: str
    stl_path: Path
    gcode_path: Path
    threemf_path: Path | None
    manifest_path: Path
    manifest: dict[str, object]
    registration: RegistrationGateResult
    l2_report: L2GeometryReport
    printability_report: PrintabilityReport
    review_record: DraftAssetReviewRecord
    slicer_adapter: str
    slicer_version: str
    runtime_catalog_registered: Literal[False] = False
    release_allowed: Literal[False] = False
    product_ready: Literal[False] = False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def prepare_fallback_draft(
    deps: Dependencies,
    session: Session,
    *,
    queue: DraftReviewQueue,
    draft_asset_id: str,
) -> DraftAssetPrepareRecord:
    """실 CAD·자동 게이트·preview를 실행하고 불변 prepare 레코드를 남긴다."""

    if isinstance(deps.cad, MockMechanicalCADGenerator):
        raise FallbackDraftExecutionError(
            "prepare requires a real CAD adapter; mock CAD is fail-closed."
        )
    draft = queue.load_draft(draft_asset_id)
    source = draft.generated_openscad_source
    if source is None:
        raise FallbackDraftSourceMissingError(
            f"Draft {draft_asset_id!r} carries no generated OpenSCAD source."
        )
    prepare_id = f"prepare_{uuid4().hex}"
    durable_dir = queue.root / "prepare_artifacts" / prepare_id
    sandbox_root = getattr(deps.cad, "sandbox_root", None)
    if not isinstance(sandbox_root, Path):
        raise FallbackDraftExecutionError(
            "prepare requires the assembled CAD adapter to expose its sandbox_root."
        )
    output_dir = sandbox_root / prepare_id
    try:
        durable_dir.mkdir(parents=True, exist_ok=False)
        output_dir.mkdir(parents=True, exist_ok=False)
        mesh = await generate_manufacturing_mesh(
            deps,
            prompt=draft.originating_prompt,
            source_code=source,
            session_id=session.session_id,
            output_dir=output_dir,
            l2_failure_message="Fallback draft prepare STL failed L2 geometry validation.",
        )
        gated = gate_fallback_draft(
            draft, gate=FallbackVerificationGate(), precompiled_stl_path=mesh.cad_result.path
        )
        if not gated.verdict.automated_gate_passed:
            raise FallbackGateNotPassedError(
                f"Fallback draft prepare gate failed: {list(gated.verdict.blocking_reasons)}"
            )
        durable_stl_path = durable_dir / "prepared.stl"
        shutil.copyfile(mesh.cad_result.path, durable_stl_path)
        preview_path = durable_dir / "preview.png"
        render_stl_preview(durable_stl_path, preview_path, size=(520, 320))
        if not preview_path.is_file() or preview_path.stat().st_size <= 0:
            raise FallbackDraftExecutionError("Fallback draft preview was not created.")
        adapter_version = str(mesh.cad_result.metadata.get("openscad_version") or "unknown")
        record = DraftAssetPrepareRecord(
            prepare_id=prepare_id,
            draft_asset_id=draft_asset_id,
            source_sha256=source_digest(source),
            stl_sha256=file_sha256(durable_stl_path),
            stl_path=str(durable_stl_path.resolve()),
            preview_sha256=file_sha256(preview_path),
            preview_path=str(preview_path.resolve()),
            gate_verdict=gated.verdict.model_dump(mode="json"),
            cad_adapter=mesh.cad_result.adapter_used,
            cad_adapter_version=adapter_version,
            prepared_at=datetime.now(UTC).isoformat(),
        )
        queue.save_prepare(record)
        return record
    except Exception:
        # 실패한 prepare는 감사 정본이 아니며 재사용할 수도 없다. 성공 레코드 없이
        # 빈/부분 artifact 디렉터리를 남기지 않는 것이 이 경로의 정리 정책이다.
        shutil.rmtree(durable_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


#: 실행 source의 지문 — 게이트 시점·사람 리뷰 시점·실행 시점을 비교하는 유일한 기준.
#:
#: 여기서 **다시 계산하지 않고** 스키마 계층의 정본을 그대로 가리킨다. 사람 리뷰
#: 기록(``record_draft_review``)과 검토 콘솔도 같은 함수를 쓴다 — 계산이 두 벌이면
#: "사람이 본 source == 실행 source" 비교가 영원히 불일치한다. 반대 방향
#: (``modules``가 ``apps``를 import)은 계층 역의존이므로 정본은 아래에 둔다.
#: 기존 공개명은 소비자 계약이라 alias로 보존한다.
source_digest = generated_source_sha256


def recheck_sandbox_tokens(scad_source: str) -> None:
    """실행 **직전** source에 sandbox ADR 토큰 검사를 다시 적용한다.

    게이트가 이미 검사했는데도 또 하는 이유: draft는 디스크의 JSON 파일이고,
    게이트 통과 시점과 실행 시점 사이에 **누구나 편집할 수 있다**. 게이트 결과를
    믿고 실행하면 "게이트를 통과한 적 있는 파일"이 "지금 안전한 파일"로 둔갑한다.
    이 경로가 실행하는 것은 임의 코드이고 재검사 비용은 거의 0이다.
    """

    if _EXTERNAL_FILE_ACCESS_RE.search(scad_source):
        raise FallbackDraftSandboxViolationError(
            "Fallback draft source references forbidden OpenSCAD external-file "
            "directives (include/use/import/surface) at execution time."
        )


def registration_printability_status(
    report: PrintabilityReport,
) -> DecorativeAssetPrintabilityStatus:
    """등록 게이트용 printability 상태를 **실제 report에서만** 도출한다.

    인자로 받지 않는 이유: 호출자가 report와 다른 값을 주면 등록 판정이 조용히
    BLOCKED되거나(진짜 통과했는데 막힘) 잘못 통과한다(막혀야 하는데 통과). 값의
    출처가 하나면 그 불일치가 표현 불가능해진다.
    """

    if report.status is PrintabilityStatus.SMOKE_PASSED and report.can_slice:
        return DecorativeAssetPrintabilityStatus.SMOKE_PASSED
    # 방어적 분기: 공통 tail의 ``require_printability_for_slicing``이 여기 오기
    # 전에 이미 막지만, "평가했고 통과 못 했다"를 ``NOT_EVALUATED``로 적으면
    # 미평가와 미통과가 같은 기록이 된다.
    return DecorativeAssetPrintabilityStatus.REVIEW_REQUIRED


async def execute_promoted_fallback_draft(
    deps: Dependencies,
    session: Session,
    *,
    queue: DraftReviewQueue,
    draft_asset_id: str,
    prepare_id: str,
) -> FallbackDraftExecutionResult:
    """사람이 승인한 prepare STL을 SHA 대조한 뒤 Orca로 실행한다.

    Args:
        queue: draft/리뷰 정본 저장소. 실행 source는 **여기서 다시 읽은** 것이다.
        draft_asset_id: 실행할 큐 draft의 식별자.
        prepare_id: 자동 게이트 산출물과 사람 리뷰를 결속하는 불변 prepare 식별자.
            정책 판단 5개와 digest는 호출 인자가 아니라 ``current_review()``가
            고른 단일 리뷰 레코드에서만 읽는다.

    Raises:
        FallbackGateNotPassedError,
        FallbackDraftSourceMissingError, FallbackDraftSourceMismatchError,
        FallbackDraftReviewMissingError, FallbackDraftReviewNotAcceptedError,
        FallbackDraftReviewSourceUnboundError,
        FallbackDraftReviewSourceMismatchError,
        FallbackDraftPrepareBindingError,
        FallbackDraftPolicyJudgmentMissingError,
        FallbackDraftPreparedArtifactMismatchError,
        FallbackDraftSandboxViolationError: [CAD 전] 전제조건 위반 —
            ``deps.cad.generate``는 호출되지 않는다.
        FallbackDraftNotRegistrationCandidateError: [CAD 후] 등록 후보 아님 —
            ``deps.slicer.slice``는 호출되지 않는다.

    Note:
        사람 리뷰 ↔ source 결속은 ``DraftAssetReviewRecord.reviewed_source_sha256``이
        소유한다. 그 값은 리뷰 기록 팩토리가 draft에서 직접 계산하고(호출자가 주입
        불가) 여기서 실행 source의 digest와 **같은 함수로** 대조된다. 따라서 이
        함수가 강제하는 불변은 "게이트가 본 source == 실행 source"에 더해
        **"사람이 검토한 source == 실행 source"**다. (사람이 그 형상을 제대로
        판단했는지는 여전히 주장하지 않는다 — 그것은 자동 게이트 밖의 축이다.)

        verdict ↔ source 결속은 :class:`GatedFallbackDraft`가 소유한다. 그 기록을
        :func:`gate_fallback_draft`로 만들면 게이트에 들어간 source와 digest가
        같은 draft에서 나오므로 결속이 구성상 성립한다. 그 기록을 손으로 위조하는
        경우까지는 타입으로 막을 수 없지만, 그때도 실행 시점 sandbox 재검사는
        **여전히 실행 source에** 걸린다(이중 방어).
    """

    # ---- [CAD 실행 전] 전제조건 4종. 하나라도 깨지면 CAD 0회. ----------------
    prepare = queue.load_prepare(prepare_id)
    if prepare.draft_asset_id != draft_asset_id:
        raise FallbackDraftPrepareBindingError(
            f"Prepare {prepare_id!r} belongs to {prepare.draft_asset_id!r}, not {draft_asset_id!r}."
        )
    verdict = FallbackGateVerdict.model_validate(prepare.gate_verdict)
    if not verdict.automated_gate_passed:
        raise FallbackGateNotPassedError(
            "Fallback draft execution requires a passing automated gate verdict; "
            f"blocking reasons: {list(verdict.blocking_reasons)}."
        )

    # 실행 정본은 게이트가 들고 있던 사본이 아니라 **지금 큐에 있는 것**이다.
    # 게이트 사본을 실행하면 디스크 재검사가 아무것도 지키지 못한다.
    current_draft = queue.load_draft(draft_asset_id)
    execution_source = current_draft.generated_openscad_source
    if execution_source is None:
        raise FallbackDraftSourceMissingError(
            f"Draft {draft_asset_id!r} carries no generated OpenSCAD source; only "
            "capable-model generated drafts are executable on this path."
        )

    execution_sha256 = source_digest(execution_source)
    if prepare.source_sha256 != execution_sha256:
        raise FallbackDraftSourceMismatchError(
            "Fallback draft source is not the source the automated gate judged "
            f"(gated sha256={prepare.source_sha256}, "
            f"queued sha256={execution_sha256}); the draft must pass the gate "
            "again before execution."
        )

    # 사람 리뷰는 **자신이 어떤 source를 보고 내린 판단인지** 기록한다
    # (``DraftAssetReviewRecord.reviewed_source_sha256``). 아래 세 검사가 합쳐져
    # "사람이 검토한 source == 실행 source"를 강제한다: ① 리뷰가 있고 ② ACCEPT이며
    # ③ 그 리뷰가 결속한 digest가 실행 source의 digest와 같다. digest가 없는(결속
    # 이전 형식) ACCEPT는 통과시키지 않는다 — 봐주면 갭이 그대로 남는다.
    review_records = queue.load_reviews(draft_asset_id)
    latest_review = current_review(review_records)
    if latest_review is None:
        raise FallbackDraftReviewMissingError(
            f"Draft {draft_asset_id!r} has no human review record; the human "
            "reviewer is the acceptance authority for fallback output."
        )
    if latest_review.decision is not DraftReviewDecision.ACCEPT:
        raise FallbackDraftReviewNotAcceptedError(
            f"Latest human review for draft {draft_asset_id!r} is "
            f"{latest_review.decision.value!r}, not 'accept'.",
            decision=latest_review.decision,
        )

    # 게이트 이후 파일이 안전해졌다고 가정하지 않는다(defense in depth).
    # sandbox 재검사를 리뷰 결속 대조보다 **먼저** 두는 이유: 실행 직전 source가
    # 금지 지시어를 담고 있다는 사실은 어떤 리뷰 상태와도 무관하게 가장 강한 차단
    # 사유이고, 그 판정이 다른 사유에 가려지면 안 된다. 둘 다 ``cad.generate`` 전이다.
    recheck_sandbox_tokens(execution_source)

    reviewed_sha256 = latest_review.reviewed_source_sha256
    if reviewed_sha256 is None:
        raise FallbackDraftReviewSourceUnboundError(
            f"Latest human review for draft {draft_asset_id!r} predates review↔source "
            "binding: it does not record which source the reviewer judged, so it "
            "cannot authorize this execution. The draft must be reviewed again "
            "against the current source."
        )
    if reviewed_sha256 != execution_sha256:
        raise FallbackDraftReviewSourceMismatchError(
            "Fallback draft source is not the source the human reviewer judged "
            f"(reviewed sha256={reviewed_sha256}, queued sha256={execution_sha256}); "
            "the draft must be reviewed again before execution."
        )
    policy_values = (
        latest_review.visual_quality_status,
        latest_review.legal_review_status,
        latest_review.commercial_allowed,
        latest_review.redistribution_allowed,
        latest_review.modification_allowed,
    )
    if any(value is None for value in policy_values):
        raise FallbackDraftPolicyJudgmentMissingError(
            "Latest ACCEPT is missing one or more explicit human policy judgments."
        )
    visual_quality_status = cast(VisualQualityStatus, latest_review.visual_quality_status)
    legal_review_status = cast(DecorativeAssetLegalReviewStatus, latest_review.legal_review_status)
    commercial_allowed = cast(bool, latest_review.commercial_allowed)
    redistribution_allowed = cast(bool, latest_review.redistribution_allowed)
    modification_allowed = cast(bool, latest_review.modification_allowed)
    if latest_review.reviewed_prepare_id != prepare_id:
        raise FallbackDraftPrepareBindingError(
            "CLI prepare_id does not match current_review().reviewed_prepare_id."
        )
    if latest_review.reviewed_artifact_sha256 != prepare.stl_sha256:
        raise FallbackDraftPreparedArtifactMismatchError(
            "Current review artifact digest does not match the prepare record."
        )

    # ---- 제조 실행. 여기부터는 artifact/manifest를 남긴다. -------------------
    artifact_root = get_artifact_root().resolve()
    execution_id = uuid4().hex[:12]
    subtask_id = f"{draft_asset_id}-{execution_sha256[:12]}-{execution_id}"
    output_dir = artifact_root / session.session_id / subtask_id
    output_dir.mkdir(parents=True, exist_ok=True)

    await deps.sessions.publish(
        session.session_id,
        make_event(
            session,
            SessionEventKind.SUBTASK_STARTED,
            payload={
                "subtask_id": subtask_id,
                "kind": "fallback_draft_asset",
                "stage": EXECUTION_STAGE,
                "draft_asset_id": draft_asset_id,
                "generated_openscad_source_sha256": execution_sha256,
            },
        ),
    )

    try:
        prepared_stl_path = Path(prepare.stl_path)
        if not prepared_stl_path.is_file():
            raise FallbackDraftPreparedArtifactMismatchError(
                f"Prepared STL is missing: {prepared_stl_path}"
            )
        l2_report = evaluate_assembly_stl(prepared_stl_path)
        if not l2_report.passed:
            raise FallbackDraftPreparedArtifactMismatchError(
                "Prepared STL no longer passes L2 geometry validation."
            )
        printability_report = evaluate_printability(l2_report)
        require_printability_for_slicing(printability_report)

        # [CAD 후 · slice 전] 실제 report로만 등록 후보를 판정한다.
        registration = promote_fallback_draft(
            _execution_time_outcome(verdict, current_draft),
            review_records=review_records,
            visual_quality_status=visual_quality_status,
            legal_review_status=legal_review_status,
            printability_status=registration_printability_status(printability_report),
            printability_report=printability_report,
            commercial_allowed=commercial_allowed,
            redistribution_allowed=redistribution_allowed,
            modification_allowed=modification_allowed,
        )
        if registration.status is not RegistrationGateStatus.CANDIDATE:
            raise FallbackDraftNotRegistrationCandidateError(
                f"Draft {draft_asset_id!r} is not a registration candidate; "
                f"blocked reasons: "
                f"{[reason.value for reason in registration.blocked_reasons]}.",
                result=registration,
            )

        if file_sha256(prepared_stl_path) != latest_review.reviewed_artifact_sha256:
            raise FallbackDraftPreparedArtifactMismatchError(
                "Prepared STL changed after review; slicing is refused."
            )
        slice_result = await slice_manufacturing_mesh(
            deps,
            mesh_path=prepared_stl_path,
            session_id=session.session_id,
            output_dir=output_dir,
        )
        manifest_stl_path = output_dir / prepared_stl_path.name
        shutil.copyfile(prepared_stl_path, manifest_stl_path)

        artifact_metadata = _fallback_artifact_metadata(
            draft=current_draft,
            execution_id=execution_id,
            source_sha256=execution_sha256,
            verdict=verdict,
            review_record=latest_review,
            review_count=len(review_records),
            registration=registration,
            l2_report=l2_report,
            printability_report=printability_report,
            visual_quality_status=visual_quality_status,
            legal_review_status=legal_review_status,
            commercial_allowed=commercial_allowed,
            redistribution_allowed=redistribution_allowed,
            modification_allowed=modification_allowed,
            slicer_adapter=slice_result.adapter_used,
            slicer_version=slice_result.slicer_version,
            gcode_size_bytes=slice_result.gcode_path.stat().st_size,
        )
        artifact_refs: list[ArtifactRef] = [
            build_artifact_ref(
                kind=ArtifactKind.MECHANICAL_MESH,
                path=manifest_stl_path,
                session_id=session.session_id,
                subtask_id=subtask_id,
                producer_adapter=prepare.cad_adapter,
                producer_version=prepare.cad_adapter_version,
                root=artifact_root,
                metadata=artifact_metadata,
            ),
            build_artifact_ref(
                kind=ArtifactKind.GCODE,
                path=slice_result.gcode_path,
                session_id=session.session_id,
                subtask_id=subtask_id,
                producer_adapter=slice_result.adapter_used,
                producer_version=slice_result.slicer_version,
                root=artifact_root,
                metadata=artifact_metadata,
            ),
        ]
        if slice_result.threemf_path is not None:
            artifact_refs.append(
                build_artifact_ref(
                    kind=ArtifactKind.THREEMF,
                    path=slice_result.threemf_path,
                    session_id=session.session_id,
                    subtask_id=subtask_id,
                    producer_adapter=slice_result.adapter_used,
                    producer_version=slice_result.slicer_version,
                    root=artifact_root,
                    metadata=artifact_metadata,
                )
            )
        manifest = SubtaskArtifactManifest(
            session_id=session.session_id,
            subtask_id=subtask_id,
            artifacts=tuple(artifact_refs),
            created_at=datetime.now(UTC),
            status=ManifestStatus.SUCCESS,
            trace_id=f"trace_item3_fallback_draft_{execution_id}",
        )
        manifest_path = write_manifest(manifest, root=artifact_root)
        manifest_payload: dict[str, object] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        failure_path = write_failure_manifest(
            session_id=session.session_id,
            subtask_id=subtask_id,
            stage=EXECUTION_STAGE,
            error_type=type(exc).__name__,
            detail=str(exc) or type(exc).__name__,
            error_metadata={
                "draft_asset_id": draft_asset_id,
                "generated_openscad_source_sha256": execution_sha256,
                "execution_id": execution_id,
            },
            trace_id=session.trace_id,
            root=artifact_root,
        )
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.SUBTASK_FAILED,
                payload={
                    "subtask_id": subtask_id,
                    "kind": "fallback_draft_asset",
                    "stage": EXECUTION_STAGE,
                    "error_type": type(exc).__name__,
                    "detail": str(exc) or type(exc).__name__,
                    "draft_asset_id": draft_asset_id,
                    "manifest_path": str(failure_path),
                },
            ),
        )
        # curated 경로처럼 단일 ``SessionStateError``로 감싸지 **않는다**:
        # 감싸면 "등록 후보 아님"과 "Orca가 mock을 냈다"가 호출자에게 같은
        # 사건으로 보이고, 어떤 전제가 깨졌는지 구분 불가해진다.
        raise

    await deps.sessions.publish(
        session.session_id,
        make_event(
            session,
            SessionEventKind.SUBTASK_COMPLETED,
            payload={
                "subtask_id": subtask_id,
                "kind": "fallback_draft_asset",
                "stage": EXECUTION_STAGE,
                "draft_asset_id": draft_asset_id,
                "manifest": manifest_payload,
            },
        ),
    )

    return FallbackDraftExecutionResult(
        draft_asset_id=draft_asset_id,
        subtask_id=subtask_id,
        source_sha256=execution_sha256,
        stl_path=prepared_stl_path,
        gcode_path=slice_result.gcode_path,
        threemf_path=slice_result.threemf_path,
        manifest_path=manifest_path,
        manifest=manifest_payload,
        registration=registration,
        l2_report=l2_report,
        printability_report=printability_report,
        review_record=latest_review,
        slicer_adapter=slice_result.adapter_used,
        slicer_version=slice_result.slicer_version,
    )


def _execution_time_outcome(
    verdict: FallbackGateVerdict,
    current_draft: DraftAssetEntry,
) -> FallbackGateOutcome:
    """등록 판정에 **실행 시점 draft**를 싣는다(verdict는 그대로).

    등록 게이트는 지금 실행되는 그 draft를 판정해야 한다. 게이트 시점 사본을
    넘기면, 판정 대상과 실행 대상이 서로 다른 객체가 되는 틈이 열린다. source
    동일성은 이미 SHA-256으로 못박혀 있고, ``DraftAssetEntry`` 자신의 validator가
    큐에서 읽히는 순간 closed-state(미등록·미릴리스)를 재확인한다.
    """

    return FallbackGateOutcome(verdict=verdict, draft=current_draft)


def _fallback_artifact_metadata(
    *,
    draft: DraftAssetEntry,
    execution_id: str,
    source_sha256: str,
    verdict: FallbackGateVerdict,
    review_record: DraftAssetReviewRecord,
    review_count: int,
    registration: RegistrationGateResult,
    l2_report: L2GeometryReport,
    printability_report: PrintabilityReport,
    visual_quality_status: VisualQualityStatus,
    legal_review_status: DecorativeAssetLegalReviewStatus,
    commercial_allowed: bool,
    redistribution_allowed: bool,
    modification_allowed: bool,
    slicer_adapter: str,
    slicer_version: str,
    gcode_size_bytes: int,
) -> dict[str, object]:
    """fallback 경로가 **자기 것으로** 만드는 manifest metadata.

    curated 경로의 ``decorative_asset_manifest_metadata``/manual-visual-review
    형태를 흉내내지 않는다. 같은 모양을 쓰면 fallback 산출물이 curated asset의
    의미(선별된 카탈로그 항목, 시각 리뷰 후보)를 가장하게 된다.
    """

    return {
        "phase": "adr_0015_item_3",
        "route": "capable_model_fallback_draft",
        "stage": EXECUTION_STAGE,
        "external_asset": False,
        "draft_asset_id": draft.draft_asset_id,
        "asset_lifecycle_status": draft.asset_lifecycle_status.value,
        "draft_generator": draft.draft_generator,
        "draft_renderer_version": draft.draft_renderer_version,
        "originating_prompt": draft.originating_prompt,
        "provenance_note": draft.provenance_note,
        "source_type": draft.source_type.value,
        "license": draft.license,
        "source_url_or_owner": draft.source_url_or_owner,
        "generated_openscad_source_sha256": source_sha256,
        # fallback ADR Item 5(D10): provenance를 **실행 manifest까지** 전파한다. ADR이
        # 요구한 것은 "record agent/model/version + generated vs cached **in the
        # manifest**"이고, draft에만 두면 절반만 닫힌다 — 실행 artifact를 나중에 보는
        # 사람은 어떤 provider·model이 그 SCAD를 만들었는지, 그게 재현 가능한 것인지
        # 알 수 없다.
        "generator_provider": draft.generator_provider,
        "generator_model": draft.generator_model,
        "scad_dsl": draft.scad_dsl,
        "scad_cache_key": draft.scad_cache_key,
        "scad_origin": draft.scad_origin,
        # 소비자가 "provider를 안 실었다"와 "provider를 **알 수 없다**"를 구분할 수
        # 있어야 한다. 판정은 스키마 계층의 정본 helper가 소유한다 — 여기서 자체
        # None-count 로직을 가지면 판정이 두 벌이 되어 영원히 불일치한다.
        #
        # legacy manifest는 **재현 가능성을 주장하지 않는다**: ``scad_cache_key``가
        # null이고 ``provenance_status``가 "legacy"인 것이 그 주장이다. unverified
        # draft도 key가 null이지만 status는 "complete"라 **구조적으로 구별된다** —
        # key만 보면 둘이 같아 보인다.
        "provenance_status": generated_draft_provenance_status(draft),
        "automated_gate": {
            "automated_gate_passed": verdict.automated_gate_passed,
            # 자동 게이트는 구조적으로 기능 정합성을 증명하지 못한다.
            "functional_correctness_certified": (verdict.functional_correctness_certified),
            "gate_gaps": list(verdict.gate_gaps),
        },
        "human_review": {
            "decision": review_record.decision.value,
            "reviewer": review_record.reviewer,
            "reviewed_at": review_record.reviewed_at,
            "review_note": review_record.review_note,
            # 이 실행이 "사람이 검토한 그 source"였음을 artifact에도 남긴다. 위의
            # ``generated_openscad_source_sha256``과 같아야 하며, 실행 경로가 그
            # 동일성을 강제한다(다르면 여기까지 오지 못한다).
            "reviewed_source_sha256": review_record.reviewed_source_sha256,
            "reviewed_artifact_sha256": review_record.reviewed_artifact_sha256,
            "reviewed_prepare_id": review_record.reviewed_prepare_id,
            "review_count": review_count,
            "scope": ("registration candidacy only; not visual product PASS, not release approval"),
        },
        "registration_gate": {
            "status": registration.status.value,
            "blocked_reasons": [reason.value for reason in registration.blocked_reasons],
            "metadata": registration.metadata,
        },
        "reviewed_policy_evidence": {
            "visual_quality_status": visual_quality_status.value,
            "legal_review_status": legal_review_status.value,
            "commercial_allowed": commercial_allowed,
            "redistribution_allowed": redistribution_allowed,
            "modification_allowed": modification_allowed,
        },
        "l2_report": l2_report.model_dump(mode="json"),
        "printability_report": printability_report.model_dump(mode="json"),
        # fallback ADR 3번: fallback 산출물은 결코 verified asset이 아니다.
        "product_ready": False,
        "release_allowed": False,
        "runtime_catalog_registered": False,
        "fallback_draft_execution": {
            "execution_id": execution_id,
            "slicer_adapter": slicer_adapter,
            "slicer_version": slicer_version,
            "gcode_size_bytes": gcode_size_bytes,
        },
    }


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "EXECUTION_STAGE",
    "FallbackDraftExecutionError",
    "FallbackDraftExecutionResult",
    "FallbackDraftNotRegistrationCandidateError",
    "FallbackDraftReviewMissingError",
    "FallbackDraftReviewNotAcceptedError",
    "FallbackDraftReviewSourceMismatchError",
    "FallbackDraftReviewSourceUnboundError",
    "FallbackDraftPrepareBindingError",
    "FallbackDraftPolicyJudgmentMissingError",
    "FallbackDraftPreparedArtifactMismatchError",
    "FallbackDraftSandboxViolationError",
    "FallbackDraftSourceMismatchError",
    "FallbackDraftSourceMissingError",
    "FallbackGateNotPassedError",
    "GatedFallbackDraft",
    "execute_promoted_fallback_draft",
    "prepare_fallback_draft",
    "file_sha256",
    "gate_fallback_draft",
    "recheck_sandbox_tokens",
    "registration_printability_status",
    "source_digest",
]
