"""Deterministic static complexity pre-cap for LLM-generated OpenSCAD source.

ADR-0015 Open Item 7: a freeform agent is far likelier than a template to emit
compute-bombs (fragment explosion, giant loops, runaway recursion). A wall-clock
timeout alone does not stop an OOM before it happens, so this guard rejects
suspicious source *before* it ever reaches the sandbox ``run()`` entrypoint.

This is a first-line, conservative, text-based heuristic — **not** a parser. It
prefers false-blocks (reject on suspicion) over false-passes (let a bomb run).
Runtime Docker memory/cpu/pids enforcement is a separate, still-open layer and is
intentionally out of scope here.
"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, ConfigDict, Field

# 초기 보수값 — fixture 측정 후 조정. 임계치는 모듈 상수로 노출한다.
MAX_FN_PER_CALL = 200
MIN_FA = 0.5
MIN_FS = 0.1
MAX_LOOP_ITERATION_PRODUCT = 100_000
MAX_NESTING_DEPTH = 50

# reason_code 우선순위(결정론). 첫 번째로 발견된 카테고리를 대표 코드로 쓴다.
_REASON_PRECEDENCE = (
    "fn_cap",
    "fa_floor",
    "fs_floor",
    "loop_product",
    "nesting_depth",
    "recursion",
)

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
# 숫자 문법(부호·지수·선행점) — $fn(cap)·$fa/$fs(floor) 모두 공유. 지수표기($fn=1e3, $fa=1e-3)나
# 선행점(.01)을 못 잡으면 폭탄을 오캡처/누락해 false-pass가 난다(독립 리뷰 P1/capstone).
_FLOAT = r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_FN_RE = re.compile(rf"\$fn\s*=\s*({_FLOAT})")
_FA_RE = re.compile(rf"\$fa\s*=\s*({_FLOAT})")
_FS_RE = re.compile(rf"\$fs\s*=\s*({_FLOAT})")
_RANGE_RE = re.compile(
    r"\[\s*(-?[0-9]+(?:\.[0-9]+)?)\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*"
    r"(?::\s*(-?[0-9]+(?:\.[0-9]+)?)\s*)?\]"
)
_LOOP_RE = re.compile(r"\b(?:intersection_for|for)\s*\(")
_MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_]\w*)\s*\(")


class ComplexityLimits(BaseModel):
    """check_scad_complexity 임계치 오버라이드(미지정 시 모듈 상수 기본값)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_fn_per_call: int = Field(default=MAX_FN_PER_CALL, gt=0)
    min_fa: float = Field(default=MIN_FA, gt=0)
    min_fs: float = Field(default=MIN_FS, gt=0)
    max_loop_iteration_product: int = Field(default=MAX_LOOP_ITERATION_PRODUCT, gt=0)
    max_nesting_depth: int = Field(default=MAX_NESTING_DEPTH, gt=0)


class ComplexityVerdict(BaseModel):
    """정적 복잡도 사전캡 결과. ok=False면 run() 전에 거부한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    violations: tuple[str, ...]
    reason_code: str


def check_scad_complexity(
    source: str, limits: ComplexityLimits | None = None
) -> ComplexityVerdict:
    """OpenSCAD 소스를 정적으로 검사해 compute-bomb 신호를 거부한다.

    결정론 텍스트 휴리스틱(파서 아님). 주석은 먼저 제거한다(주석 속 위험 토큰은
    실행되지 않으므로 false-block을 줄인다). 변수로 경계가 정해진 루프/난독화된
    소스는 추정 대상이 아니다(NOT CLAIMED — 모듈 docstring 참조).
    """

    active = limits if limits is not None else ComplexityLimits()
    code = _strip_comments(source)

    categorized: list[tuple[str, str]] = []
    categorized.extend(_check_fn(code, active.max_fn_per_call))
    categorized.extend(_check_fa_fs(code, active.min_fa, active.min_fs))
    categorized.extend(_check_loops(code, active.max_loop_iteration_product))
    categorized.extend(_check_nesting(code, active.max_nesting_depth))
    categorized.extend(_check_recursion(code))

    violations = tuple(message for _, message in categorized)
    present = {category for category, _ in categorized}
    reason_code = next((code_ for code_ in _REASON_PRECEDENCE if code_ in present), "")
    return ComplexityVerdict(
        ok=not violations, violations=violations, reason_code=reason_code
    )


def _strip_comments(source: str) -> str:
    without_block = _BLOCK_COMMENT_RE.sub("", source)
    return _LINE_COMMENT_RE.sub("", without_block)


def _check_fn(code: str, max_fn: int) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for raw in _FN_RE.findall(code):
        # float로 비교한다 — int(float(raw))는 $fn=1e400(=inf)에서 OverflowError로
        # 호출자를 터뜨려 false-block 불변을 깬다(독립 리뷰 적발). 표기 시에만 유한성 가드.
        value = float(raw)
        if value > max_fn:
            shown: int | float = int(value) if math.isfinite(value) else value
            findings.append(("fn_cap", f"fn_cap:$fn={shown}>{max_fn}"))
    return findings


def _check_fa_fs(code: str, min_fa: float, min_fs: float) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for raw in _FA_RE.findall(code):
        value = float(raw)
        if value < min_fa:
            findings.append(("fa_floor", f"fa_floor:$fa={value:g}<{min_fa:g}"))
    for raw in _FS_RE.findall(code):
        value = float(raw)
        if value < min_fs:
            findings.append(("fs_floor", f"fs_floor:$fs={value:g}<{min_fs:g}"))
    return findings


def _check_loops(code: str, max_product: int) -> list[tuple[str, str]]:
    product = 1
    matched_any = False
    for match in _LOOP_RE.finditer(code):
        body = _balanced_span(code, match.end() - 1, "(", ")")
        if body is None:
            continue
        for start, mid, end in _RANGE_RE.findall(body):
            count = _range_iteration_count(start, mid, end)
            if count is None:
                continue
            matched_any = True
            product *= count
    if matched_any and product > max_product:
        return [("loop_product", f"loop_product:{product}>{max_product}")]
    return []


def _check_nesting(code: str, max_depth: int) -> list[tuple[str, str]]:
    depth = 0
    deepest = 0
    for char in code:
        if char == "{":
            depth += 1
            deepest = max(deepest, depth)
        elif char == "}":
            depth = max(0, depth - 1)
    if deepest > max_depth:
        return [("nesting_depth", f"nesting_depth:{deepest}>{max_depth}")]
    return []


def _check_recursion(code: str) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for match in _MODULE_RE.finditer(code):
        name = match.group(1)
        brace = code.find("{", match.end())
        if brace == -1:
            continue
        body = _balanced_span(code, brace, "{", "}")
        if body is None:
            continue
        if re.search(rf"\b{re.escape(name)}\s*\(", body):
            findings.append(("recursion", f"recursion:module '{name}' calls itself"))
    return findings


def _range_iteration_count(start: str, mid: str, end: str) -> int | None:
    """[start:end] 또는 [start:step:end]의 반복 횟수를 보수적으로 추정한다."""

    if end:
        low, step, high = float(start), float(mid), float(end)
    else:
        low, step, high = float(start), 1.0, float(mid)
    if step == 0.0:
        return None
    span = (high - low) / step
    if span < 0:
        return None
    return int(span) + 1


def _balanced_span(code: str, open_index: int, open_ch: str, close_ch: str) -> str | None:
    """open_index의 여는 괄호에 대응하는 닫는 괄호까지의 내부 문자열을 반환한다."""

    if open_index >= len(code) or code[open_index] != open_ch:
        return None
    depth = 0
    for index in range(open_index, len(code)):
        char = code[index]
        if char == open_ch:
            depth += 1
        elif char == close_ch:
            depth -= 1
            if depth == 0:
                return code[open_index + 1 : index]
    return None
