# claude-print-proxy

`claude -p`(Claude Code CLI print 모드)를 백엔드로 쓰는 OpenAI 호환 프록시.
Honcho의 OpenAI-compatible 모델 설정에서 `BASE_URL` 로 지정해 쓴다.
외부 의존성 없음(Node ESM).

## 특징

- 모델 **opus** 고정. effort 는 서버 기본값(`CLAUDE_PROXY_EFFORT`, 기본 `low`)을
  쓰고, 요청 본문 `reasoning_effort`(`low|medium|high|xhigh|max`)가 오면 그 호출만
  덮어쓴다. 잘못된 값은 400. 요청의 `model`, `temperature`, `max_tokens` 는
  무시한다(`model` 은 응답에 echo만 되고, 없으면 `claude-opus-5-<effort>`).
- 호출마다 깨끗한 상태로 실행: CLAUDE.md, 설정 파일, MCP, 내장 툴, 세션 저장을
  모두 끄고 빈 작업 디렉터리에서 돌린다.
- OpenAI `tools` / `tool_choice` 와 `response_format`(json_schema, json_object)을
  `--json-schema` 구조화 출력으로 에뮬레이션한다.
- `stream: true` 는 전체 결과를 받은 뒤 SSE 두 청크 + `[DONE]` 으로 보낸다(부분 스트리밍 없음).

## 요구 사항

로컬 `claude` CLI(2.1.272 이상)가 OAuth 로그인된 상태여야 한다(`claude /login`).
`--bare` 는 OAuth를 읽지 않으므로 사용하지 않는다.

## 실행

```bash
cd claude-print-proxy
node server.mjs            # 기본 127.0.0.1:11436
PORT=11446 node server.mjs
node --test server.test.mjs
```

주의: 이 머신에서는 127.0.0.1:11436 을 ssh 터널이 이미 쓰고 있다.
Honcho에 연결할 때는 `PORT` 를 바꾸거나 터널을 옮겨야 한다.

## 이 머신의 상주 실행 (2026-09-15)

LaunchAgent `com.chenjing.claude-print-proxy`가 `127.0.0.1:11446`에서 상시 실행한다(KeepAlive, 로그인 시 시작).
로그는 `~/.hermes/logs/claude-print-proxy.log` / `.error.log`, Bearer 시크릿은
`~/dev/ubionRAG/.secrets/claude-print-proxy.key`에 있다. 이 디렉터리의
`com.chenjing.claude-print-proxy.plist`는 시크릿 자리에 `__SECRET__`을 둔 템플릿이며,
설치된 사본(`~/Library/LaunchAgents/`)에만 실제 값이 있다. 재설치 방법은 템플릿 안 주석 참고.
소비처: WeKnora(ubionRAG) 모델 `claude-opus-5 [low]`가 `http://host.docker.internal:11446/v1`로 호출한다.

## 환경 변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `PORT` | `11436` | 리슨 포트 |
| `HOST` | `127.0.0.1` | 루프백이 아니면 공유 시크릿 필수 |
| `CLAUDE_PROXY_SHARED_SECRET` | 없음 | 설정 시 `Authorization: Bearer <secret>` 검사 |
| `CLAUDE_PROXY_EFFORT` | `low` | 기본 effort. `low|medium|high|xhigh|max` 외면 시작 시 종료 |
| `CLAUDE_BIN` | `claude` | CLI 실행 파일 |
| `CLAUDE_PROXY_MAX_CONCURRENCY` | `4` | 동시 자식 프로세스 수 |
| `CLAUDE_PROXY_REQUEST_TIMEOUT_MS` | `600000` | 초과 시 자식 kill 후 504 |
| `CLAUDE_PROXY_WORKDIR` | 임시 디렉터리 | 자식 프로세스 cwd |
| `CLAUDE_PROXY_MAX_BODY_BYTES` | 8 MiB | 요청 본문 상한 |
| `CLAUDE_PROXY_MAX_IMAGE_BYTES` | `20971520` (20 MiB) | 이미지 1장 상한(data URL, http 모두). 초과 시 400 |
| `CLAUDE_PROXY_MAX_IMAGES` | `10` | 요청당 이미지 수 상한. 초과 시 400 |
| `CLAUDE_PROXY_REQUEST_LOG` | `1` | `0`이면 요청별 로그 줄을 끈다 |

data URL 이미지는 본문에 base64로 실리므로 `CLAUDE_PROXY_MAX_BODY_BYTES`(8 MiB)에도
걸린다. 실제로 6 MiB 이상 이미지를 받으려면 둘 다 올려야 한다.

## claude 호출 인자

```
claude -p --model opus --effort <effort> --tools "" --no-session-persistence \
  --setting-sources "" --strict-mcp-config --max-turns 1 --output-format json \
  --system-prompt <system> [--json-schema <schema>]
```

프롬프트는 stdin으로 넘긴다. 자식 env에서 `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` 를
제거해 중첩 세션 판정을 피한다.

이미지가 포함된 요청은 인자가 달라진다(이미지가 없으면 위 인자 그대로, 바이트 단위로 동일):

```
claude -p --model opus --tools Read --allowedTools Read --no-session-persistence \
  --setting-sources "" --strict-mcp-config --max-turns <이미지 수 + 2> --output-format json \
  --effort <effort> --system-prompt <system> [--json-schema <schema>]
```

cwd는 요청별 이미지 디렉터리(`<workdir>/req-XXXXXX/`)라서 Read가 작업 디렉터리를
벗어나지 않는다. `--permission-mode`, `--add-dir`, 권한 우회 플래그는 필요 없다
(2.1.272에서 확인, `permission_denials: []`).

## 이미지 입력 (2026-09-16)

OpenAI `content` 배열의 `image_url` 파트(data URL base64, http(s) URL; `detail`은 무시)와
`input_image` 파트를 받는다. `claude -p`는 stdin으로 이미지를 못 받으므로 요청마다
`CLAUDE_PROXY_WORKDIR` 아래 `req-XXXXXX/`를 만들어 `image-1.png`, `image-2.jpg` … 로
저장(0600)하고, 그 디렉터리를 자식 cwd로 삼아 절대경로를 프롬프트에 넣어 Read 도구로
읽게 한다. 응답 후에는 성공·502·타임아웃·클라이언트 중단 모두 디렉터리를 삭제한다.

- 지원 포맷: png, jpeg, gif, webp. 확장자는 선언된 MIME이나 URL 경로가 아니라
  매직바이트로 정한다(클라이언트가 MIME을 잘못 붙이는 경우가 많다). 선언 MIME이
  네 가지 밖이면 즉시 400.
- 400이 되는 경우: base64 아님, 이미지가 아닌 바이트, `data:application/pdf`, `file://`
  등 http(s) 외 스킴, http 가져오기 실패, 상한 초과.
- 프롬프트 형식: 이미지 파트 자리에 `[Image N]`, 맨 끝에 `# Images` 섹션과
  `Image N: <절대경로>` 목록, "답하기 전에 모두 Read하라"는 지시.
- `system`/`developer` 메시지 안의 이미지는 버린다. 시스템 프롬프트는 CLI 인자라
  Read 대상이 될 수 없다. 이 경우 요청은 텍스트 전용 호출로 처리된다.
- 이미지·이외의 비텍스트 파트는 `[unsupported content part omitted]`로 렌더링된다.
- 이미지 저장(http 가져오기 포함)은 동시성 세마포어 안에서 일어나므로
  `CLAUDE_PROXY_MAX_CONCURRENCY`가 디스크에 동시에 존재하는 이미지 수도 제한한다.
- http(s) URL에 SSRF 필터는 없다. 루프백 바인드 + Bearer 시크릿이라 호출자를 신뢰하는
  전제이며, 외부에 노출하려면 allow-list가 필요하다.

지연 시간(effort low, 2026-09-16 로컬 실측): 텍스트만 3.1초, 이미지 1장 5.0초, 2장 5.8초.
이미지 1장이면 prompt_tokens가 3.5k 정도로 뛴다(Read 도구 정의 + 이미지 토큰).

운영 반영(11446 LaunchAgent)은 벤치마크 등 진행 중인 작업이 없을 때:

```
cd ~/dev/honcho/claude-print-proxy && node --test server.test.mjs
launchctl kickstart -k gui/$(id -u)/com.chenjing.claude-print-proxy
curl -s http://127.0.0.1:11446/health
```

plist 수정은 필요 없다(새 변수는 기본값으로 동작). 상한을 바꾸려면 plist의
`EnvironmentVariables`에 변수를 넣고 템플릿 주석대로 재설치한 뒤 `kickstart -k`.

## 요청 로그 (2026-09-16)

chat-completions 요청 1건마다 stdout에 JSON 한 줄을 남긴다(LaunchAgent 설정에서는
`~/.hermes/logs/claude-print-proxy.log`). `/health`와 404는 남기지 않는다. 프롬프트·응답 본문·헤더·
시크릿은 절대 기록하지 않는다(테스트로 확인). 벤치마크의 호출 수·토큰 집계 용도.

필드는 항상 존재하고 모르면 `null`: `event`(`"request"`), `at`(완료 시각 ISO), `status`(HTTP,
`499`는 클라이언트가 먼저 끊은 경우), `model`(응답에 돌려준 모델 문자열), `effort`, `images`(장 수),
`tools`, `json_schema`(bool), `num_turns`, `duration_ms`(프록시 벽시계, 동시성 대기 포함),
`claude_duration_ms`(CLI 보고값), `usage`(`prompt_tokens`, `completion_tokens`, `total_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`), `cost_usd`, `error`(`bad_request` |
`unauthorized` | `payload_too_large` | `client_closed_request` | `upstream_error` | `timeout` |
`server_error`, 성공 시 `null`). `is_error`로 502가 난 호출도 쓴 토큰은 기록된다.

집계 예시(기간을 자르려면 `select(.at >= "2026-09-16T00:00:00Z")` 추가):

```
jq -s 'map(select(.event=="request")) | {calls:length, ok:(map(select(.status==200))|length),
  failed:(map(select(.status!=200))|length), prompt_tokens:(map(.usage.prompt_tokens//0)|add),
  completion_tokens:(map(.usage.completion_tokens//0)|add), total_tokens:(map(.usage.total_tokens//0)|add),
  cache_read:(map(.usage.cache_read_input_tokens//0)|add), cost_usd:(map(.cost_usd//0)|add),
  proxy_ms:(map(.duration_ms//0)|add)}' ~/.hermes/logs/claude-print-proxy.log
```

로그 파일은 회전하지 않는다(호출당 350바이트 정도).

## 요청 변환

- `system`/`developer` 메시지를 합쳐 `--system-prompt` 로 보낸다.
- user 메시지 하나뿐이면 그대로 프롬프트, 그 외에는 `<conversation>` transcript로
  렌더링한다(assistant `tool_calls`, `tool` 결과 포함).
- `tools` 가 있으면 시스템 프롬프트에 툴 정의를 덧붙이고
  `{content, tool_calls[]}` 스키마를 강제한다. 결과는 OpenAI `tool_calls` 형식으로
  변환되고 `finish_reason` 은 `tool_calls`.
- `response_format.json_schema` 는 스키마를 그대로 `--json-schema` 로 전달한다.

## 엔드포인트와 상태 코드

- `GET /health` → `{status, model, effort, max_concurrency}` (effort 는 서버 기본값)
- `POST /v1/chat/completions` (또는 `/chat/completions`)
- 400 잘못된 본문, 401 시크릿 불일치, 502 CLI 오류(`is_error`, 비정상 종료,
  파싱 실패), 504 타임아웃. 오류 본문은 `{error:{message,type}}`.

## Honcho 연결 예시

```
DERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL=http://host.docker.internal:11446/v1
DERIVER_MODEL_CONFIG__OVERRIDES__API_KEY_ENV=LLM_VLLM_API_KEY
```

호출당 지연은 약 3.5~5초이고, CLI 자체 시스템 프롬프트 때문에 사소한 요청도
prompt_tokens 가 400~1300 정도 잡힌다.
