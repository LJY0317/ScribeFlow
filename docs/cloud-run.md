# ScribeFlow on Google Cloud Run

이 구성은 **선택한 Google Drive 음성 파일 하나**를 Cloud Run Job에서
OpenAI `gpt-transcribe`로 처리하고, 동명 SHA-256 TXT를 같은 Drive 폴더에
저장합니다. Cloud Run Service는 인증된 ChatGPT가 요청할 수 있는
Streamable HTTP MCP 서버를 제공합니다.

## 먼저 확인할 사항

- Google Cloud **결제 연결된 프로젝트**가 필요합니다. Cloud Run 무료 제공량이
  있어도 Cloud Build·Artifact Registry·Secret Manager·네트워크 및 API 전사에는
  별도 비용이 생길 수 있습니다. Google Cloud 예산 알림은 **하드 상한이 아닙니다**.
- 클라우드 전사는 **새 실행 컨테이너**에서 처리되므로 로컬 실패 캐시를
  공유하지 않습니다. 실행 중 실패한 녹음은 다시 실행하면 API 비용이
  중복될 수 있습니다. Job의 자동 재시도는 0으로 설정합니다.
- Google Drive 접근에 사용할 OAuth refresh token과 OpenAI API 키는
  **Secret Manager**에만 넣습니다. GitHub나 Cloud Run 환경변수에 평문으로
  직접 저장하지 마세요.
- Auth0는 별도 인증 제공자입니다. **무료 플랜 가능 여부와 MCP 기능 제공 여부를
  확인**하고, 유료로 전환하지 않도록 설정합니다. Auth0 없이 접근 권한을
  열면 누구나 전사 API 비용을 발생시킬 수 있습니다.

## 1. Google Cloud Shell에서 초기 설정

Google Cloud Console에서 프로젝트 생성·결제 연결 후 Cloud Shell을 엽니다.
Cloud Shell에는 `gcloud`가 설치돼 있으므로 맥북에 SDK를 설치할 필요가
없습니다. 공개 소스만 복제합니다.

```bash
git clone https://github.com/LJY0317/ScribeFlow.git
cd ScribeFlow
export GCP_PROJECT='your-cloud-project-id'
export SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID='folder_id_for_audio_scope'
# 선택: 추가로 전사가 허용된 여러 폴더 ID를 콜론(:)으로 나열
export SCRIBEFLOW_ALLOWED_FOLDER_IDS='another_folder_id:one_more_folder_id'
export SCRIBEFLOW_OAUTH_ISSUER='https://your-tenant.us.auth0.com/'
export SCRIBEFLOW_ALLOWED_SUBJECT='your-auth0-user-subject'
# 선택: 이미 만든 Secret Manager 항목 이름이 기본값과 다른 경우
export SCRIBEFLOW_OPENAI_SECRET='existing-openai-secret-name'
export SCRIBEFLOW_RCLONE_SECRET='existing-rclone-secret-name'
```

Cloud Shell과 ChatGPT 대화에 API 키 또는 Drive 토큰을 붙여 넣지 마세요.

## 2. Secret Manager

Console → Security → Secret Manager에서 다음 두 secret을 만듭니다.
사용자 인증이 필요한 데이터는 이 저장소에 업로드하지 않습니다.

- `scribeflow-openai-api-key`: ScribeFlow 전용 OpenAI 프로젝트 API 키
- `scribeflow-rclone-config`: `[gdrive]` 연결만 포함한 rclone 설정 파일의
  텍스트. 개인 맥의 전체 rclone 설정을 통째로 복사하지 마세요. 이 설정에는
  OAuth refresh token이 포함되므로 **비공개 유지**가 필수입니다.

기존 Secret Manager 항목을 재사용하는 경우 이름을 변경하거나 복제하지 말고
`SCRIBEFLOW_OPENAI_SECRET`와 `SCRIBEFLOW_RCLONE_SECRET`에 해당 이름을 설정하세요.

Drive 인증이 만료·폐기되면 이 secret을 업데이트해야 합니다. Job은
실행 중 쓰기 가능한 사본만 사용하며 변경 내용을 Secret Manager에
역으로 기록하지 않습니다.

## 3. 클라우드 배포

```bash
bash scripts/deploy-cloud-run.sh
```

스크립트는 최소한의 전사 Job 및 MCP Service를 만들고 역할을 분리합니다.
MCP Service는 처음에는 **Cloud Run IAM 비공개 상태**로 배포됩니다.
Cloud Shell의 `gcloud` 인증 및 충분한 IAM 권한이 필요합니다.

## 4. Auth0 연동

Auth0에서 MCP용 API를 하나 만들고 **API Identifier**를 출력된
`https://<service-url>/mcp`와 정확히 동일하게 설정합니다(RS256).

- 별도 Auth0 tenant를 사용하고, Dynamic Client Registration(DCR)과
  Resource Parameter Compatibility Profile을 활성화합니다.
- API에 `transcribe:write` 권한을 정의하고, 사용할 본인 계정에만 부여합니다.
- `SCRIBEFLOW_ALLOWED_SUBJECT`는 Auth0 사용자 ID(`sub`)와 정확히
  일치해야 합니다. 임의의 로그인 가능 사용자에게 작업 권한을 주지 않습니다.
- ChatGPT에서 연결 유형을 OAuth로 선택하고 Auth0의 DCR 흐름을 사용합니다.
  Auth0/ChatGPT의 CIMD 등록 방식은 호환성 문제가 있을 수 있으므로
  실제 연결 테스트가 필요합니다.

Auth0 설정을 완료했을 때만 공개 HTTPS 엔드포인트를 엽니다.

```bash
bash scripts/enable-mcp-access.sh
```

이 스크립트는 익명 요청이 **HTTP 401**로 차단되는지 확인합니다.
검증에 실패하면 다시 IAM 비공개 상태로 변경합니다.

ChatGPT 웹의 Plugins에서 `Add custom MCP server`로
`https://<service-url>/mcp`를 추가해 OAuth 로그인을 완료합니다.
이후 해당 플러그인에서 `transcribe_audio(file_id)`를 사용합니다.
기본 폴더 외 음성은 `transcribe_audio(file_id, folder_id)`처럼 폴더 ID를
함께 전달할 수 있지만, 서버에 설정한 **허용된 폴더만** 접근할 수 있습니다.
반환된 `operation`을 `get_transcription_status(operation)`에
전달해 완료 여부를 확인하세요. 완료된 TXT는 원본 Google Drive 폴더에
저장됩니다. 실행 로그 링크는 인증된 Google Cloud 콘솔에서 볼 수 있습니다.

## 보안·정리

- 전사 요청은 사용자가 지정한 **개별 Drive 파일 ID**만 받습니다. 음악
  폴더 전체의 자동 전사나 무인 스캔은 하지 않습니다.
- 서비스의 최소 인스턴스 수는 0, 최대는 1입니다. Job은 1작업/실행이고
  자동 재시도는 하지 않지만, 사용자가 여러 실행을 요청하면 각각 과금됩니다.
- Cloud Run, Artifact Registry 이미지, Secret Manager, 전사 비용은 별도
  점검하세요. 예산 알림을 설정하되 강제 결제 상한으로 오해하지 마세요.
- 사용 중단 시 MCP 연결을 해제하고 `gcloud run services delete scribeflow-mcp`,
  `gcloud run jobs delete scribeflow-transcribe`로 계산 자원을 정리합니다.
  두 secret과 서비스 계정, 컨테이너 이미지는 **공유 사용 여부 확인 후**
  개별적으로 정리합니다. Google Drive의 원본과 TXT는 삭제하지 않습니다.
