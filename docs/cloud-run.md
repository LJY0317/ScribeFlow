# ScribeFlow on Google Cloud Run

ChatGPT와 Google Cloud만 이용하는 개인용 원격 전사 구성입니다. 사용자 요청으로 지정한
Google Drive 음성 파일 **하나**를 Cloud Run Job이 OpenAI `gpt-transcribe`로 전사하고
원본과 같은 Drive 폴더에 SHA-256 검증용 TXT를 저장합니다.

## 구성과 주의사항

- 신규 인증 업체(Auth0 등)는 필요하지 않습니다. 다만 ChatGPT의 *No authentication*
  MCP 연결은 임의 API 키 헤더를 지원하지 않아, 별도로 생성한 **추측 불가능한
  비밀 URL**을 서버 접근 키처럼 사용합니다.
- **URL을 아는 사람은 누구나 전사를 요청할 수 있습니다.** URL을 절대 채팅,
  GitHub, 공개 스크린샷 등에 게시하지 마세요. 유출 시 Secret Manager의
  `scribeflow-mcp-access-token` 비밀을 교체한 뒤 서비스를 재배포하고
  ChatGPT 연결도 갱신해야 합니다. URL 경로는 Google Cloud 요청 로그에도
  나타날 수 있습니다.
- 이 방식은 Google 계정의 사용자를 직접 검증하는 OAuth가 아닙니다.
  본인만 사용하는 개인 플러그인용 절충안입니다.
- Cloud Run Service는 0개의 최소 인스턴스, 1개의 최대 인스턴스이며 Job은
  1회 1작업, 자동 재시도 0입니다. Job 배포만으로 음성 전사를 시작하지 않습니다.
- Cloud Build, Artifact Registry, Secret Manager, Cloud Run, OpenAI API 사용료가
  발생할 수 있습니다. Google Cloud 예산은 OpenAI API 비용을 제한하지 않습니다.

## 사전 준비

Google Cloud Shell에서 프로젝트를 선택하고 ScribeFlow 코드를 받습니다.

```bash
git clone https://github.com/LJY0317/ScribeFlow.git
cd ScribeFlow
export GCP_PROJECT='YOUR_PROJECT_ID'
# 이미 만들어 둔 Secret Manager 이름이 기본값과 다르면 설정
export SCRIBEFLOW_OPENAI_SECRET='YOUR_OPENAI_SECRET_NAME'
export SCRIBEFLOW_RCLONE_SECRET='YOUR_RCLONE_SECRET_NAME'
```

Secret Manager에 미리 등록할 항목은 다음 두 개입니다.

- OpenAI API 키 (기본 이름: `scribeflow-openai-api-key`)
- `[gdrive]` 항목만 포함하는 rclone 설정 (기본 이름: `scribeflow-rclone-config`).
  기존 rclone 파일의 다른 연결/토큰은 공유하지 마세요.

## 배포

```bash
bash scripts/deploy-cloud-run.sh
```

스크립트는 이미지 빌드, Cloud Run 전사 Job, **IAM 비공개** MCP 서비스를 만들고,
추가로 MCP용 48자 정도의 임의 비밀값을 Secret Manager에 저장합니다.
아직 사용자나 ChatGPT가 외부에서 접근할 수 없습니다.

다음 검사를 통과한 경우에만 외부 접근을 활성화합니다.

```bash
bash scripts/enable-mcp-access.sh
```

- 평범한 `/mcp` 주소는 HTTP **404**가 나와야 합니다.
- 비밀 URL 주소는 MCP `initialize` 응답으로 HTTP **200**이 나와야 합니다.
- 검사 실패 시 `allUsers` 호출 권한을 다시 제거합니다.
- 성공 후 출력된 비밀 URL은 **ChatGPT 플러그인 설정란에만 복사**하세요.

ChatGPT 웹 > Plugins > Add custom MCP server를 선택하고 비밀 URL의 전체
`https://.../<비밀값>/mcp` 주소를 입력하세요. 인증 유형은
`No authentication`입니다. 플러그인을 설치하고 `@`로 호출할 수 있습니다.
사용 도구는 `transcribe_audio(file_id)`와
`get_transcription_status(operation)`입니다.

## Drive 범위 및 안전한 운영

- 특별한 루트 폴더 제한 없이 연결된 `gdrive:`의 전체 루트에서
  요청한 파일 ID를 찾습니다. 전체 Drive를 자동으로 전사하지는 않습니다.
- 파일 ID 검색은 재귀적으로 파일을 조회하므로 큰 Drive에서는 느릴 수 있습니다.
  별도 공유 드라이브나 '공유 문서함' 전체를 자동 포함하지는 않습니다.
- API 호출 후 실패한 음성을 다시 실행하면 비용이 다시 발생할 수 있습니다.
- Google Drive OAuth 토큰은 Cloud Run Job의 임시 쓰기 가능한 설정 파일로 복사합니다.
  rclone의 토큰이 갱신된 경우 Secret Manager 원본이 자동 갱신되지는 않습니다.
- 중단하려면 Cloud Run 서비스의 `allUsers` invoker 바인딩을 제거하세요.
  더 이상 사용하지 않으면 서비스와 Job 및 관련 비밀과 이미지를 각각 정리합니다.
  원본 녹음이나 결과 TXT를 지우지 않습니다.
