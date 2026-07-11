# cad_mechanical sandbox

`DockerSandboxRunner`가 사용하는 Docker image asset의 placeholder입니다.

Phase 4는 code와 test에서 execution boundary를 고정합니다.

- `SandboxRunner`가 execution boundary를 정의합니다.
- `RejectAllRunner`는 기본적으로 execution을 거부합니다.
- `DockerSandboxRunner`는 isolated Docker invocation을 구성합니다.
- Docker image는 non-`latest` tag 또는 sha256 digest로 pin해야 합니다.
- `request.output_dir`는 configured `sandbox_root` 아래의 per-session/job child여야 합니다.
  넓은 host directory를 writable mount하면 안 됩니다.
- Real adapter는 `exec()` 또는 host Python subprocess를 직접 호출하면 안 됩니다.

Sandbox policy는 `docs/decisions/ADR-0001-cad-sandbox.md`에 고정되어 있습니다.
