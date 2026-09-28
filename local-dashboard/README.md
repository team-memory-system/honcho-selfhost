# Honcho Local Dashboard

Self-hosted Honcho v3를 위한 로컬 관리 대시보드입니다. 외부 의존성 없이 Node.js 20 이상에서 실행됩니다.

```bash
cd local-dashboard
HONCHO_URL=http://127.0.0.1:8001 npm start
```

브라우저에서 `http://127.0.0.1:4173`을 여세요.

이 대시보드를 launchd 서비스로 등록해 두면 Honcho API·MCP와 함께 로그인 시 자동 시작하고, 종료 시 자동 재시작합니다. 라벨은 설치마다 다릅니다.

환경 변수:

- `HONCHO_URL`: Honcho API 주소 (기본값 `http://127.0.0.1:8001`)
- `HONCHO_API_KEY`: 인증이 켜진 서버의 API 키
- `DASHBOARD_PORT`: 대시보드 포트 (기본값 `4173`)
- `DASHBOARD_HOST`: 바인딩 주소 (기본값 `127.0.0.1`)
- `MCP_CONTROL_DRY_RUN`: `1`이면 launchd와 설정 파일을 건드리지 않고 MCP 도구 스위치 UI만 시험
- `HONCHO_MCP_TOOL_CONFIG`: MCP 도구 설정 파일 경로 (기본값 `~/.config/honcho/mcp-bridge/tool-config.json`)
- `MCP_CONTROL_MODE=file`: Docker 배포처럼 MCP 프로세스는 호스트가 관리하고, 대시보드는 공유 설정 파일만 수정할 때 사용
- `MCP_CONTROL_ALLOW_REMOTE=1`: 컨테이너에서 들어오는 도구 설정 요청을 허용합니다. 대시보드 포트가 `127.0.0.1`에만 공개된 경우에만 사용하세요.

API 인증이 필요하면 `HONCHO_API_KEY` 환경 변수로 설정합니다.

Dialectic 화면은 두 모드를 지원합니다.

- `단일 Peer`: 선택한 Peer의 기억에 직접 질문
- `Peer 통합`: 선택한 모든 Observer Peer가 Focus Peer에 대해 답한 뒤 Focus Peer가 하나의 답으로 종합

사이드바의 `MCP 도구` 화면에서는 공식 Honcho 도구 30개와 로컬 `server_info`를 포함한 31개 도구를 각각 켜거나 끌 수 있습니다.
Bridge의 도구 노출은 대시보드 설정 파일로만 제어하며 조회·쓰기·삭제·LLM 도구의 성격을 화면에서 구분합니다.
변경 시 bridge만 재시작하며 Honcho API와 tunnel은 유지합니다. 재시작할 launchd 라벨은 `HONCHO_BRIDGE_LAUNCHD_LABEL` 로 지정합니다.
MCP 도구 제어 API는 localhost 요청만 허용합니다.
