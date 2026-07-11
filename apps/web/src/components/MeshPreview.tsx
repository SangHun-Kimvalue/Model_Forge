"use client";

import { useEffect, useMemo, useState } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import type { BufferGeometry, Group, Material } from "three";
import { Box3, Mesh, MeshStandardMaterial, Vector3 } from "three";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";

import {
  selectLatestMeshArtifact,
  useOrchestratorStore,
  type ArtifactEntry,
} from "@/stores/orchestrator-store";
import { Panel } from "./Panel";
import styles from "./MeshPreview.module.css";

// Hoisted to module scope — prevents React from recreating these objects on every render.
const CAMERA_CONFIG = { position: [3, 3, 3] as [number, number, number], fov: 50 };
const LIGHT_POSITION: [number, number, number] = [5, 5, 5];
const MESH_ROTATION: [number, number, number] = [0.35, 0.5, 0];
const BOX_ARGS: [number, number, number] = [1.5, 1.5, 1.5];
const PREVIEW_MATERIAL = new MeshStandardMaterial({ color: "#4f8cff" });

/**
 * Mesh preview backed by the ADR-0005 artifact manifest.
 *
 * Prefers mechanical STL because it is the manufacturable slicer input.
 * Falls back to organic OBJ when no mechanical mesh exists. Both formats
 * are loaded from the manifest-backed `/artifacts` route.
 *
 * The geometry is normalized to a unit-ish bounding box and centered
 * on the origin so very small or very large meshes still fit the
 * orbital camera without requiring per-mesh camera tuning.
 */
export function MeshPreview(): JSX.Element {
  const artifact = useOrchestratorStore(selectLatestMeshArtifact);
  return (
    <Panel title="모델 미리보기" testId="mesh-preview">
      <PreviewNote artifact={artifact} />
      <div className={styles.canvasWrap} data-testid="mesh-canvas-wrap">
        <Canvas camera={CAMERA_CONFIG}>
          <ambientLight intensity={0.55} />
          <directionalLight position={LIGHT_POSITION} intensity={0.8} />
          {artifact ? (
            <PreviewMesh artifact={artifact} />
          ) : (
            <mesh rotation={MESH_ROTATION}>
              <boxGeometry args={BOX_ARGS} />
              <meshStandardMaterial color="#4f8cff" />
            </mesh>
          )}
          <OrbitControls enablePan={false} />
        </Canvas>
      </div>
    </Panel>
  );
}

function PreviewNote({ artifact }: { artifact: ArtifactEntry | null }): JSX.Element {
  if (!artifact) {
    return (
      <div className={styles.note} data-testid="mesh-preview-note">
        <span>아직 생성된 모델이 없어 임시 모델을 표시합니다.</span>
      </div>
    );
  }
  return (
    <div className={styles.note} data-testid="mesh-preview-note">
      <span>
        {artifact.label} ({artifact.kind})
      </span>
    </div>
  );
}

/**
 * Fetches and renders an STL or OBJ artifact. Geometry/group state lives in
 * a hook so R3F can re-render when a new artifact arrives without remounting
 * the Canvas.
 */
function PreviewMesh({ artifact }: { artifact: ArtifactEntry }): JSX.Element {
  const [geometry, setGeometry] = useState<BufferGeometry | null>(null);
  const [object, setObject] = useState<Group | null>(null);
  const [error, setError] = useState<string | null>(null);
  const format = useMemo<"stl" | "obj" | "unsupported">(
    () =>
      artifact.relativeUri.toLowerCase().endsWith(".stl")
        ? "stl"
        : artifact.relativeUri.toLowerCase().endsWith(".obj")
          ? "obj"
          : "unsupported",
    [artifact.relativeUri],
  );

  useEffect(() => {
    if (format === "unsupported") {
      setGeometry(null);
      setObject(null);
      setError(null);
      return;
    }
    let cancelled = false;
    if (format === "obj") {
      const loader = new OBJLoader();
      loader.load(
        artifact.url,
        (group) => {
          if (cancelled) {
            disposeGroup(group);
            return;
          }
          normalizeObject(group);
          setObject(group);
          setGeometry(null);
          setError(null);
        },
        undefined,
        (err) => {
          if (cancelled) return;
          setObject(null);
          setGeometry(null);
          setError(err instanceof Error ? err.message : String(err));
        },
      );
      return () => {
        cancelled = true;
      };
    }

    const loader = new STLLoader();
    loader.load(
      artifact.url,
      (geo) => {
        if (cancelled) {
          geo.dispose();
          return;
        }
        normalizeGeometry(geo);
        geo.computeVertexNormals();
        setGeometry(geo);
        setObject(null);
        setError(null);
      },
      undefined,
      (err) => {
        if (cancelled) return;
        setGeometry(null);
        setObject(null);
        setError(err instanceof Error ? err.message : String(err));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [artifact.url, format]);

  // Dispose previous geometry/object on replace/unmount.
  useEffect(() => {
    return () => {
      geometry?.dispose();
      if (object) disposeGroup(object);
    };
  }, [geometry, object]);

  if (error || (format === "stl" && !geometry) || (format === "obj" && !object)) {
    return <PlaceholderMesh error={Boolean(error)} />;
  }

  if (object) return <primitive object={object} />;
  if (!geometry) return <PlaceholderMesh error={false} />;

  return (
    <mesh geometry={geometry}>
      <meshStandardMaterial color="#4f8cff" />
    </mesh>
  );
}

function PlaceholderMesh({ error }: { error: boolean }): JSX.Element {
  return (
    <mesh rotation={MESH_ROTATION}>
      <boxGeometry args={BOX_ARGS} />
      <meshStandardMaterial
        color={error ? "#ff6b6b" : "#7a8496"}
        wireframe={!error}
      />
    </mesh>
  );
}

function normalizeGeometry(geo: BufferGeometry): void {
  geo.computeBoundingBox();
  const box = geo.boundingBox ?? new Box3();
  const center = new Vector3();
  box.getCenter(center);
  geo.translate(-center.x, -center.y, -center.z);
  const size = new Vector3();
  box.getSize(size);
  const maxDim = Math.max(size.x, size.y, size.z, 1e-6);
  const scale = 2 / maxDim;
  geo.scale(scale, scale, scale);
}

function normalizeObject(group: Group): void {
  group.traverse((child) => {
    if (child instanceof Mesh && !child.material) {
      child.material = PREVIEW_MATERIAL;
    }
  });
  const box = new Box3().setFromObject(group);
  const center = new Vector3();
  box.getCenter(center);
  group.position.sub(center);
  const size = new Vector3();
  box.getSize(size);
  const maxDim = Math.max(size.x, size.y, size.z, 1e-6);
  const scale = 2 / maxDim;
  group.scale.setScalar(scale);
  group.rotation.set(...MESH_ROTATION);
}

function disposeGroup(group: Group): void {
  group.traverse((child) => {
    const maybeMesh = child as { geometry?: BufferGeometry; material?: Material | Material[] };
    maybeMesh.geometry?.dispose();
    if (Array.isArray(maybeMesh.material)) {
      maybeMesh.material.forEach((material) => material.dispose());
    } else if (maybeMesh.material && maybeMesh.material !== PREVIEW_MATERIAL) {
      maybeMesh.material.dispose();
    }
  });
}
