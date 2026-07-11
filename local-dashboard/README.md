# Honcho Local Dashboard

Self-hosted Honcho v3를 위한 로컬 관리 대시보드입니다. 외부 의존성 없이 Node.js 20 이상에서 실행됩니다.

```bash
cd local-dashboard
HONCHO_URL=http://127.0.0.1:8001 npm start
```

브라우저에서 `http://127.0.0.1:4173`을 여세요.

이 Mac에서는 `com.chenjing.honcho-dashboard` LaunchAgent가 Honcho API·MCP와 함께 로그인 시 자동 시작하고, 종료 시 자동 재시작합니다.

환경 변수:

- `HONCHO_URL`: Honcho API 주소 (기본값 `http://127.0.0.1:8001`)
- `HONCHO_API_KEY`: 인증이 켜진 서버의 API 키
- `DASHBOARD_PORT`: 대시보드 포트 (기본값 `4173`)
- `DASHBOARD_HOST`: 바인딩 주소 (기본값 `127.0.0.1`)
- `MCP_CONTROL_DRY_RUN`: `1`이면 launchd와 설정 파일을 건드리지 않고 MCP 도구 스위치 UI만 시험
- `HONCHO_MCP_TOOL_CONFIG`: MCP 도구 설정 파일 경로 (기본값 `~/.hermes/local-honcho-mcp/tool-config.json`)

API 인증이 필요하면 `HONCHO_API_KEY` 환경 변수로 설정합니다.

Dialectic 화면은 두 모드를 지원합니다.

- `단일 Peer`: 선택한 Peer의 기억에 직접 질문
- `Peer 통합`: 선택한 모든 Observer Peer가 Focus Peer에 대해 답한 뒤 Focus Peer가 하나의 답으로 종합

사이드바의 `MCP 도구` 화면에서는 공식 Honcho 도구 30개와 로컬 `server_info`를 포함한 31개 도구를 각각 켜거나 끌 수 있습니다.
Bridge의 도구 노출은 대시보드 설정 파일로만 제어하며 조회·쓰기·삭제·LLM 도구의 성격을 화면에서 구분합니다.
변경 시 `com.chenjing.honcho-external-mcp` bridge만 재시작하며 Honcho API와 tunnel은 유지합니다.
MCP 도구 제어 API는 localhost 요청만 허용합니다.
