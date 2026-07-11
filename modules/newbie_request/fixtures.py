from __future__ import annotations

from modules.newbie_request.schemas import (
    Anchor,
    AssemblyComponent,
    AssemblySpec,
    QualityPolicy,
    SizeMM,
    VisualQualityStatus,
)


def bear_keyring_assembly_spec() -> AssemblySpec:
    return AssemblySpec(
        object_type="decorative_keyring",
        category="decorative_keyring",
        base_shape="rounded_tag",
        source_catalog_id="bear_keyring",
        source_prompt_terms=("곰돌이", "키링"),
        size_mm=SizeMM(width=55, depth=48, height=4),
        anchors=(
            Anchor(id="center", role="center", x_mm=27.5, y_mm=24, z_mm=0),
            Anchor(id="top", role="top", x_mm=27.5, y_mm=43, z_mm=0),
            Anchor(id="face", role="face", x_mm=27.5, y_mm=24, z_mm=3),
        ),
        components=(
            AssemblyComponent(id="base", component_type="base_rounded_tag", anchor_id="center"),
            AssemblyComponent(id="head", component_type="bear_head", anchor_id="center"),
            AssemblyComponent(id="ears", component_type="ears", anchor_id="top"),
            AssemblyComponent(id="eyes", component_type="eye_dots", anchor_id="face"),
            AssemblyComponent(id="nose", component_type="nose_muzzle", anchor_id="face"),
            AssemblyComponent(id="loop", component_type="keyring_hole", anchor_id="top"),
        ),
        required_features=(
            "bear_head",
            "ears",
            "eyes",
            "nose_or_muzzle",
            "keyring_hole",
        ),
        quality_policy=QualityPolicy(
            visual_quality_required=True,
            visual_quality_status=VisualQualityStatus.NOT_EVALUATED,
        ),
    )


def rabbit_keyring_assembly_spec() -> AssemblySpec:
    return AssemblySpec(
        object_type="decorative_keyring",
        category="decorative_keyring",
        base_shape="rounded_tag",
        source_catalog_id="rabbit_keyring",
        source_prompt_terms=("토끼", "키링"),
        size_mm=SizeMM(width=55, depth=52, height=4),
        anchors=(
            Anchor(id="center", role="center", x_mm=27.5, y_mm=24, z_mm=0),
            Anchor(id="top", role="top", x_mm=27.5, y_mm=47, z_mm=0),
            Anchor(id="face", role="face", x_mm=27.5, y_mm=24, z_mm=3),
        ),
        components=(
            AssemblyComponent(id="base", component_type="base_rounded_tag", anchor_id="center"),
            AssemblyComponent(id="head", component_type="rabbit_head", anchor_id="center"),
            AssemblyComponent(id="ears", component_type="rabbit_ears", anchor_id="top"),
            AssemblyComponent(id="eyes", component_type="eye_dots", anchor_id="face"),
            AssemblyComponent(id="nose", component_type="nose_muzzle", anchor_id="face"),
            AssemblyComponent(id="loop", component_type="keyring_hole", anchor_id="top"),
        ),
        required_features=(
            "rabbit_head",
            "ears",
            "eyes",
            "nose_or_muzzle",
            "keyring_hole",
        ),
        quality_policy=QualityPolicy(
            visual_quality_required=True,
            visual_quality_status=VisualQualityStatus.NOT_EVALUATED,
        ),
    )


__all__ = ["bear_keyring_assembly_spec", "rabbit_keyring_assembly_spec"]
