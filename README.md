# Honcho Selfhost

공식 Honcho를 고정된 서브모듈로 가져오고, 필요한 수정만 패치로 적용하는 개인 기억 서버 배포 저장소입니다.

```text
upstream/honcho/       공식 plastic-labs/honcho 소스
selfhost-source.json   공식 버전·커밋과 패치 적용 순서
patches/              코어 수정과 회귀 테스트
local-mcp-bridge/      로컬 MCP, 도구 접근 제어, 조회 기록
local-dashboard/       기억 대시보드
.build/honcho/         빌드할 때 생성되는 실행 소스
```

현재 공식 기반은 **v3.2.1**입니다. 원본의 파일은 서브모듈 안에서 수정하지 않습니다.

## 소스 준비와 실행

Git, Node.js 18 이상, Docker Compose가 필요합니다.

```sh
git clone --recurse-submodules https://github.com/team-memory-system/honcho-selfhost
cd honcho-selfhost
node scripts/prepare-source.mjs
```

이미 clone한 경우에도 `prepare-source.mjs`가 빠진 서브모듈을 초기화합니다. 공식 커밋을
별도 디렉터리로 내보낸 뒤 패치를 확인·적용하고, 성공했을 때만 `.build/honcho`를 바꿉니다.
원본 체크아웃에 수정이 있거나 지정된 커밋과 다르면 중단합니다.

`.env`와 브리지 토큰 파일을 준비한 뒤 서버를 실행합니다. `HONCHO_CONFIG_DIR`는
`.env`에 반드시 지정해야 합니다. 예시는 `AGENTS.md`와 각 동반 서비스 README에 있습니다.

```sh
docker compose -f docker-compose.selfhost.yml up -d --build
```

이 명령은 운영 배포입니다. 테스트에서는 별도 이미지 이름과 임시 DB를 사용하세요.
API와 deriver는 준비된 소스 안의 **공식 Dockerfile**로 빌드합니다.

## 변경과 업데이트

- 코어를 고칠 때: 고정된 공식 소스의 임시 사본에서 수정·테스트하고 `patches/`를 갱신합니다.
- 공식 버전을 올릴 때: 아래 스크립트가 별도 후보 작업 공간에서 서브모듈 버전과 패치를 검증합니다.
- MCP와 대시보드: 각각의 디렉터리에서 관리합니다. 공식 소스에 패치할 필요가 없습니다.

```sh
scripts/prepare_upstream_update.sh v3.2.1
# 후보에서 패치와 테스트를 검증한 뒤
scripts/promote_upstream_update.sh v3.2.1
```

패치 충돌은 자동으로 숨기지 않습니다. 후보의 패치를 수정하고 테스트한 뒤 승격합니다.
승격은 Git 소스만 갱신하며 운영 컨테이너를 다시 만들지는 않습니다.

## 팀 메모리 설치기와 배포본

`honcho-agent-bridge` **0.3.5 이상**은 이 저장소를 받은 다음 서브모듈과 패치를 처리해
기존과 같은 실행 디렉터리로 설치합니다. 배포 압축파일도 준비된 실행 소스를 담습니다.
`.honcho-source.json`에 공식 커밋과 패치 체크섬이 함께 기록됩니다.
이전 플러그인으로 새 구조를 설치하기 전에 플러그인을 업데이트하세요.

## 검증

```sh
node --test tests/prepare-source.test.mjs
node scripts/prepare-source.mjs
```

Python 테스트는 `.build/honcho`에서 실행합니다. `PYTHON_DOTENV_DISABLED=1`과 별도
테스트 DB 주소를 지정해 운영 설정이 섞이지 않게 하세요. 자세한 절차는 `AGENTS.md`에 있습니다.

라이선스는 AGPL-3.0입니다. 공식 소스와 라이선스는 `upstream/honcho`에도 보존됩니다.
