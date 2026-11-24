# ScribeFlow 원격 설치 · 사용 · 완전 제거

Google Cloud Run에 개인용 MCP 서버와 요청당 실행되는 음성 전사 Job을 배포합니다. ChatGPT에서 **Google OAuth**로 로그인하고 Google Drive 음성 파일 ID를 전달하면, OpenAI API가 음성을 전사하여 **원본과 같은 Drive 폴더에 TXT 또는 Markdown**을 만듭니다. 서버는 유휴 시 인스턴스가 0개로 줄어듭니다. **설정 후에는 맥북을 켜둘 필요도, 다른 PC에 ScribeFlow를 설치할 필요도 없습니다.**

필요한 것: Google 계정, 결제가 연결된 Google Cloud 프로젝트, [OpenAI API 키](https://platform.openai.com/api-keys), Google Drive, 사용자 지정 MCP를 지원하는 ChatGPT 계정. Google Cloud와 OpenAI API는 별도 과금합니다. **Google Cloud 예산 알림은 지출을 자동 차단하는 하드캡이 아닙니다.**

> **계정은 구분됩니다.** Cloud 프로젝트를 관리하는 Google 계정, ChatGPT에 로그인한 계정, ScribeFlow 사용을 인증할 Google 계정은 서로 달라도 됩니다. 다만 ChatGPT 계정을 타인과 공유한다면, 연결된 ScribeFlow 전사 권한도 공유될 수 있습니다. OAuth는 **최초 연결**을 보호하지만 ChatGPT 사용자를 매번 재인증하지 않습니다.

## 1. Google Cloud 프로젝트와 코드 준비

1. [Google Cloud 프로젝트 만들기](https://console.cloud.google.com/projectcreate)에서 전용 프로젝트를 생성하고 [결제](https://console.cloud.google.com/billing)를 연결합니다. 전용 프로젝트면 나중에 프로젝트 자체를 삭제하기 쉽습니다.
2. [Cloud Shell](https://shell.cloud.google.com/)을 열어 아래 명령을 실행합니다. `YOUR_PROJECT_ID`는 콘솔의 프로젝트 **ID**입니다.

```bash
export GCP_PROJECT='YOUR_PROJECT_ID'
gcloud config set project "$GCP_PROJECT"
gcloud services enable secretmanager.googleapis.com --project="$GCP_PROJECT"
git clone https://github.com/LJY0317/ScribeFlow.git ~/ScribeFlow
cd ~/ScribeFlow
```

이미 저장소가 있다면 `git clone` 대신 `cd ~/ScribeFlow && git pull --ff-only`를 사용합니다. Cloud Shell은 오래 사용하지 않으면 종료되지만, **배포된 Cloud Run 서비스와 별개**입니다. 관리자에게 Google Cloud 프로젝트 리소스·IAM 변경 권한이 필요합니다.

## 2. Google Drive 접근

원격 MCP는 사용자가 ChatGPT에서 연결한 Google 계정의 OAuth 권한으로 Drive API에 직접 접근합니다. 별도 rclone 설치·인증 파일·고정 Drive 계정 설정은 필요 없습니다. 배포 스크립트가 Google Drive API를 활성화하며, Google Auth Platform의 데이터 액세스에서 `https://www.googleapis.com/auth/drive` 범위를 설정합니다. 원본 다운로드와 최종 전사문 저장·갱신에는 해당 계정의 실제 Drive 권한이 필요합니다.

로컬 CLI에서도 동일한 Drive API 전사 코드를 사용하며, rclone은 로컬 OAuth 연결과 경로·폴더 탐색에만 필요합니다. [README의 연결 방법](../README.md)을 참고하세요.

## 3. OpenAI API 키 등록

[OpenAI API 키](https://platform.openai.com/api-keys)를 생성하고 API 결제·사용량을 설정합니다. **ChatGPT 구독료와 API 사용료는 별개**입니다. 비밀값은 아래 프롬프트에만 입력하고 터미널 출력·코드·GitHub에 남기지 마세요.

```bash
read -r -s -p 'OpenAI API key: ' OPENAI_KEY; echo
printf '%s' "$OPENAI_KEY" | gcloud secrets create scribeflow-openai-api-key \
  --replication-policy=automatic --data-file=- --project="$GCP_PROJECT"
unset OPENAI_KEY
export SCRIBEFLOW_OPENAI_SECRET='scribeflow-openai-api-key'
```

이미 Google Secret Manager에 OpenAI 키가 있다면 다시 만들지 말고 `SCRIBEFLOW_OPENAI_SECRET`에 그 비밀 이름을 설정합니다. Secret Manager의 복사본을 삭제해도 **OpenAI에서 발급한 원본 API 키는 유효하게 남습니다.**

## 4. Google OAuth 로그인 설정

아직 서버를 배포하지 않았어도 다음 명령으로 **고정형 Cloud Run 주소**를 계산할 수 있습니다.

```bash
PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')"
URL="https://scribeflow-mcp-${PROJECT_NUMBER}.asia-northeast3.run.app"
echo "MCP endpoint: $URL/mcp"
echo "OAuth redirect: $URL/auth/callback"
```

1. [Google Auth Platform](https://console.cloud.google.com/auth/overview)의 브랜딩·대상(Audience)을 설정합니다. 개인 Google 계정은 External/외부를 선택하고, 앱이 테스트 상태라면 **ScribeFlow 로그인용 Google 계정을 테스트 사용자로 추가**합니다.
2. 클라이언트 → 클라이언트 만들기 → **웹 애플리케이션**을 선택합니다. 이름은 예: `ScribeFlow`. 승인된 JavaScript 원본은 필수가 아닙니다. Google 제공 MCP 도구용 AI 에이전트 옵션도 필요하지 않습니다.
3. **승인된 리디렉션 URI**에는 출력된 `$URL/auth/callback` 주소를 정확히 등록하고 저장합니다. 이것은 ChatGPT의 MCP 주소가 **아닙니다**.
4. 발급된 Client ID와 **실제 OAuth 로그인을 허용할 Google 계정 이메일**을 다음 환경변수로 설정하고, Client Secret은 Secret Manager에 저장합니다.

```bash
export SCRIBEFLOW_GOOGLE_CLIENT_ID='YOUR_CLIENT_ID.apps.googleusercontent.com'
export SCRIBEFLOW_GOOGLE_EMAIL='YOUR_GOOGLE_LOGIN_EMAIL'

read -r -s -p 'Google OAuth Client Secret: ' GOOGLE_CLIENT_SECRET; echo
printf '%s' "$GOOGLE_CLIENT_SECRET" | gcloud secrets create scribeflow-google-client-secret \
  --replication-policy=automatic --data-file=- --project="$GCP_PROJECT"
unset GOOGLE_CLIENT_SECRET
```

Google 로그인에는 신원 확인과 Drive 접근을 위해 `openid`, `userinfo.email`, `userinfo.profile`, 전체 Drive 범위를 요청합니다. 기본값은 한 명의 허용 계정이며, 필요한 경우에만 추가 이메일을 설정합니다. 로그인 이메일을 바꾸려면 Cloud Run의 `SCRIBEFLOW_GOOGLE_EMAIL` 설정과 Google OAuth 테스트 사용자 설정을 함께 변경하고 재인증해야 합니다.

## 5. Cloud Run 배포 · ChatGPT 연결

**같은 Cloud Shell 세션**에서 프로젝트·OpenAI Secret·Google OAuth Client ID·허용 이메일을 설정하고 실행합니다. 배포 스크립트는 Drive API를 활성화하고 작업 암호화 키를 기존 Secret Manager에 생성하거나 재사용합니다.

```bash
cd ~/ScribeFlow
bash scripts/deploy-cloud-run.sh
bash scripts/enable-mcp-access.sh
```

첫 번째 스크립트는 필요한 Google Cloud API, 앱 전용 서비스 계정, `scribeflow-oauth` Firestore 데이터베이스를 설정하고 **비공개 상태**로 Cloud Run MCP 서버와 전사 Job을 배포합니다. 두 번째 스크립트는 익명 MCP 도구 요청에 **401**, OAuth 메타데이터에 **200**이 반환되는지 확인한 다음 외부 연결을 활성화합니다. `*.a.run.app`과 위 고정 주소가 달라도 Cloud Run의 주소 별칭이므로 **ChatGPT와 Google OAuth에는 위 고정 주소를 사용**합니다.

`main`에 푸시하면 GitHub Actions가 테스트를 통과한 이미지를 GitHub Container Registry에 `sha-<전체 커밋 SHA>` 태그로 게시합니다. 배포 스크립트는 현재 커밋의 공개 이미지를 사용합니다. 포크의 이미지나 정확한 이미지 다이제스트를 지정하려면 `SCRIBEFLOW_IMAGE`를 설정합니다. 처음 게시한 패키지는 기본적으로 비공개이므로 공개 배포를 하려면 GitHub **Packages → 패키지 설정 → 공개 범위**에서 Public으로 설정합니다. 실행 시 필요한 자격증명은 Secret Manager에서 주입합니다. [GitHub 패키지 공개 범위](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)

ChatGPT의 **Plugins → 사용자 지정 MCP 서버 추가**(UI 명칭은 변경 가능)에서 다음과 같이 설정합니다.

| 항목 | 입력 |
| --- | --- |
| 이름 | `ScribeFlow` |
| 서버 URL | **`https://scribeflow-mcp-프로젝트번호.asia-northeast3.run.app/mcp`** |
| 인증 | **OAuth** |

ChatGPT의 접근 허용(Allow Access)을 승인하고 **4단계의 Google 계정으로 로그인**합니다. 다른 PC·Mac에서도 **같은 ChatGPT 계정 및 워크스페이스**라면 로컬 설치는 필요 없습니다. 다만 ChatGPT 앱·플러그인 제공 상태 및 OAuth 토큰 만료에 따라 앱 선택 또는 재인증이 필요할 수 있습니다.

### 추가 계정 (선택)

기본 설치는 `SCRIBEFLOW_GOOGLE_EMAIL`의 한 계정만 허용합니다. 여러 계정을 사용하려면 Google OAuth 테스트 사용자를 추가하고 배포 전에 다음 값을 설정합니다.

```bash
export SCRIBEFLOW_GOOGLE_ADDITIONAL_EMAILS='second@example.com third@example.com'
```

추가 이메일은 공백으로 구분합니다. 허용된 사용자는 자신의 Google 계정으로 인증하고 해당 계정의 Drive 권한만 사용합니다. Worker에 전달되는 단기 Drive 자격 증명은 암호화되며 작업 시간은 최대 45분입니다. 결과 조회는 해당 작업을 요청한 계정만 할 수 있습니다. [Google Drive 범위](https://developers.google.com/workspace/drive/api/guides/api-specific-auth) · [Google 인증 검증 예외](https://support.google.com/cloud/answer/13464323)

Google Docs·Sheets·Slides 편집 및 파일 형식 변환은 기존 Google Drive 플러그인을 사용합니다. ScribeFlow는 전사와 전사 결과의 파일 ID 전달에 집중합니다.

- ChatGPT가 표시에 사용하는 `get_profile`은 실제 이메일과 Google이 제공한 이름을 반환합니다. Google이 제공한 이미지 URL은 별도 읽기 전용 도구 `get_google_profile_photo`에서 확인할 수 있습니다.
- `search_drive_files`는 일반 파일·폴더의 메타데이터를 반환하고 `nextPageToken`으로 계속 조회합니다. Google이 `incompleteSearch`를 표시하면 `include_shared_drives=false`로 개인 범위를 조회하고, `list_google_shared_drives`에서 얻은 ID로 공유 드라이브를 하나씩 조회할 수 있습니다.
- `check_drive_file`은 녹음 다운로드와 원본 폴더의 전사문 저장 가능 여부를 확인합니다. 전사를 시작하거나 파일을 쓰지 않습니다. 같은 점검을 CLI에서 하려면 `scribeflow[cloud]`를 설치하고 `scribeflow connection HTTPS_MCP_URL --file-id DRIVE_FILE_ID`로 기존 브라우저 OAuth 흐름을 사용합니다.

## 6. 사용 · 시험 · 운영

- `transcribe_audio(file_id, keywords=None, prompt="", retry=False, language="auto", output_format="txt")`: Google Drive 음성 파일 ID **한 개**를 받아 전사 작업을 시작하며 OpenAI API 사용료가 발생합니다. 기본 TXT 외에 `output_format="md"`로 Markdown 한 개를 직접 저장할 수 있습니다. 필요한 경우 `keywords`에 최대 20개의 고유명사·전문용어를, `prompt`에 500자 이내의 짧은 녹음 맥락을 전달할 수 있습니다. 실제 발언을 지어내는 프롬프트를 제공하지 마세요. **동일 파일 ID의 실행 중 전사 요청**은 Firestore에 원자적으로 예약해 기존 Cloud Run 작업을 재사용합니다. 성공한 작업이 있어도 새 요청은 현재 Drive·Worker 모델을 다시 확인합니다. 실패한 작업의 재시도에는 `retry=True`를 명시해야 합니다. 원본 SHA-256이 일치하는 기존 TXT·MD는 [공통 모델 갱신 정책](../README.md#기존-전사문과-모델-갱신-정책-cli-mcp-공통)에 따라 재사용(`skipped_existing`)하거나 지원되는 상위 모델로 갱신(`upgraded`)합니다. 갱신은 기존 형식·ID·위치를 유지하고 유료 API 호출이 발생합니다. 단, 이전 시도가 실행 여부를 확인할 수 없는 상태로 끝난 경우에는 2시간의 안전 유예시간 후에만 명시적 재시도가 가능합니다. 재시도는 API 사용료가 추가로 발생할 수 있으므로 반드시 기존 작업을 먼저 확인하세요.
- `language` 옵션: 자동 감지(`auto`)가 기본값입니다. 한국어 등 입력 언어를 아는 경우에는 `ko`, `en`과 같은 힌트를 지정할 수 있습니다. 감지 실패 시 추가 재전사나 자동 재인코딩은 수행하지 않습니다. 큰 다중 오디오 트랙은 일부 트랙 유실을 막기 위해 명시적으로 중단합니다.
- `retranscribe_audio(file_id, keywords=None, prompt="", language="auto", transcript_file_id="", output_format="txt")`: 선택한 녹음을 다시 전사해 기존 전사문을 교체합니다. `output_format="md"`면 Markdown을 대상으로 합니다. 다른 폴더로 이동한 전사문은 `transcript_file_id`에 해당 Drive 파일 ID를 명시할 수 있습니다. 전사문 말꼬리의 원본 SHA-256이 현재 오디오와 일치해야 유료 전사를 시작하며, 기존 파일 ID 및 현재 폴더는 유지됩니다. 생략 시 원본 폴더의 동명 전사문을 우선 확인합니다. 이전 내용은 [Drive 버전 보관 정책](https://developers.google.com/workspace/drive/api/guides/manage-revisions)의 적용을 받습니다. 원격 MCP는 현재 연결된 Google 계정의 권한으로 직접 갱신합니다.
- 원본 폴더에 선택 형식의 동명 전사문이 없으면 Drive에서 이름과 원본 SHA-256이 일치하는 후보 한 개만 갱신합니다. 여러 후보 또는 이름이 바뀐 파일은 `transcript_file_id`로 지정하세요. 원격 MCP에서는 원본 폴더에 접근할 수 없어도 원본을 다운로드하고 지정한 전사문을 편집할 수 있으면 재전사 가능합니다.
- `get_transcription_status(operation)`: 실행 상태(`running` / `completed` / `failed`)와 종료 결과(`result`, 실제 `output_format`, `transcript_file_id`, `transcript_url`, `model`, 자동 갱신 시 `previous_model` 또는 `error_code`)를 조회합니다. 결과가 보관되지 않은 이전 실행이나 강제 종료에서는 상태만 반환할 수 있습니다. 조회는 새 전사를 시작하지 않습니다.

**모델 변경(관리자용):** Cloud Run 콘솔에서 `scribeflow-transcribe` Job의 환경변수 `SCRIBEFLOW_TRANSCRIPTION_MODEL`에 해당 OpenAI 전사 API에서 사용할 모델 ID를 설정합니다. 기본값은 `gpt-transcribe`입니다. 현재 SDK의 전사 API·언어 및 키워드 매개변수와 호환되는 모델을 지정하세요. 고정 스냅샷 ID가 제공되면 직접 설정할 수 있습니다. 다음 실행부터 적용되며 모델 변경 자체로 전체 Drive를 갱신하지 않습니다. 사용자가 특정 파일의 전사를 요청하면 공통 정책에서 명시한 상위 모델 관계를 확인해 갱신합니다. 알려지지 않은 모델 변경은 품질 확인 후 명시적으로 재전사를 요청하세요. 모델 ID와 스냅샷이 같은 전사본의 내부 변경 이력은 TXT에서 판별할 수 없습니다.

**Drive 파일 식별:** 원격 MCP는 원본 ID로 다운로드하고, 원본의 실제 부모 ID로 신규 전사문을 저장하거나 지정한 전사문 ID로 갱신합니다. 이름만으로 중복 파일을 선택하지 않습니다. 로컬 CLI도 같은 `drive_transcription.transcribe_drive` 코드와 Drive API를 사용합니다. rclone은 로컬 OAuth와 경로 탐색에만 쓰이며 파일 전송·갱신 대체 경로는 없습니다. 두 모드 모두 게시 전 원본·갱신 대상의 변경을 확인합니다.

**중복 실행과 결과:** 기존 `scribeflow-oauth` 데이터베이스의 `scribeflow_job_claims`에 원본 ID의 해시, 작업 ID·형식·소유자와 실행별 최소 결과(파일 ID, 형식, 모델 또는 오류 코드)를 기록합니다. Worker에는 이 전용 DB에 한정된 권한을 부여합니다. 결과와 예약은 7일 후 만료되며 UTC 기준 하루 첫 전사 요청에서 최대 100개를 지웁니다. 남은 만료 항목은 이후 요청에서 추가로 정리합니다. 녹음 내용·인증 토큰은 결과 기록에 저장하지 않습니다. [Firestore 할당량](https://docs.cloud.google.com/firestore/quotas)

**이전 TTL 설정을 사용한 배포:** 새 Gateway에서 만료 예약 정리가 작동하는 것을 확인한 후, `GCP_PROJECT`를 설정하고 아래 명령으로 기존 유료 TTL 정책을 끕니다. 새 배포에는 별도 TTL 설정이 필요 없습니다.

```bash
bash scripts/disable-claim-ttl.sh
```

완료된 실행 이후의 새 요청도 Cloud Run 작업 하나를 생성해 현재 상태를 확인합니다. 일반 전사 요청은 공통 모델 갱신 정책에 해당하는 기존 TXT·MD만 갱신하고 그 외에는 보호합니다. 이전 작업의 상태 조회가 불가능할 때에는 최소 2시간 후에 명시적 재시도 또는 재전사가 가능합니다.

ID는 `https://drive.google.com/file/d/FILE_ID/view`에서 확인합니다. MCP 검색 도구 또는 기존 Google Drive 플러그인에서 파일 ID를 찾을 수 있습니다. 동명 전사문이 있으면 원본 SHA-256과 모델을 확인합니다. 지원되는 상위 모델 갱신에만 `upgraded`가 반환되고, 재사용 시 `skipped_existing`, 출처 미확인·불일치 시에는 덮어쓰지 않습니다.

**실전 검증 방법:** 원본 음성만 있고 동명 TXT가 없는 파일로 전사 요청 → `completed` 확인 → 원본 Drive 폴더의 **새 TXT 파일 ID·생성시각·본문** 확인. 기존 TXT를 시험 삼아 제거한다면 **휴지통으로 이동**해 복구 가능한 상태로 두세요. 실제 API 호출은 비용이 발생합니다.

**검증 사실(2026-11-13):** 공유받은 Drive 폴더의 M4A 약 0.84MB와 일반 Drive의 M4A 약 68MB에서 기존 TXT를 휴지통으로 옮긴 뒤 새 TXT가 생성됐습니다. Linux CI의 Python 테스트 및 Docker 빌드도 통과했습니다. Windows 로컬 CLI 및 각 모바일 ChatGPT 앱은 별도 실기 검증 전입니다.

**기존 배포 업데이트 (v0.6.1 이후):** 새 Cloud Shell이나 로컬 체크아웃에서도 기존 OAuth Client ID, 이메일, Worker 리전, OpenAI API 주소·모델 또는 Secret 이름을 다시 설정할 필요가 없습니다. GitHub에서 최신 소스를 받고 Google Cloud 프로젝트만 지정하세요.

```bash
cd ~/ScribeFlow
git switch main
git pull --ff-only
export GCP_PROJECT='YOUR_EXISTING_PROJECT_ID'
bash scripts/update-cloud-run.sh --dry-run  # 선택: 기존 대상과 새 이미지 확인
bash scripts/update-cloud-run.sh
```

업데이트는 기존 Gateway를 조회하여 현재 연결된 Worker 리전과 Job 이름을 확인하고, 두 리소스의 이미지만 변경합니다. 허용 계정·OAuth·환경변수·Secret 참조·IAM·리전·CPU·메모리 설정은 그대로 유지합니다. 여러 리전에 Gateway가 있다면 `SCRIBEFLOW_REGION`으로 기존 대상 하나를 선택하세요. 이미 전사 중인 작업은 완료 후 업데이트하는 편이 좋습니다. GitHub Actions에서 새 이미지 게시가 성공한 뒤 실행하며, 이전 이미지로 되돌릴 때에는 `SCRIBEFLOW_IMAGE`를 지정합니다.

**설정 변경 및 이전 설치의 마이그레이션:** 설정을 변경하려면 Cloud Run 콘솔 또는 `gcloud run services update` / `gcloud run jobs update`의 개별 옵션을 사용합니다. 예를 들어 추가 Google 계정이나 API 주소를 변경할 때는 해당 환경변수만 갱신하세요. 최초 설치용 `deploy-cloud-run.sh`는 기존 리소스가 있으면 정상 업데이트 안내와 함께 중단합니다. v0.6.0 이전 고정 계정 배포에서 전환하거나 설치 중단 후 구성을 다시 적용할 때만 전체 설치 설정을 명시하고 `bash scripts/deploy-cloud-run.sh --reconfigure` 및 `bash scripts/enable-mcp-access.sh`를 실행하세요. 이때 Drive OAuth 범위와 기존 Worker 리전·추가 계정·모델·API 주소·Secret 이름을 복원해야 합니다. 이전 rclone 마운트·볼륨과 불필요한 Worker의 Secret 접근 권한은 해제하되 원본 인증 비밀은 보존합니다. 이전에 Drive 권한을 승인하지 않은 연결은 재인증해야 합니다.

**배포 후 확인:** `bash scripts/update-cloud-run.sh --dry-run`으로 현재 두 이미지의 일치를 확인할 수 있습니다. 활성 리전, 저장소·버킷·TTL, 로그 보관 상태는 아래 [저비용 운영 구성](#저비용-운영-구성)을 참고하세요. 배포 자체는 OpenAI 전사 API를 호출하지 않습니다. 실제 음성을 전사하는 품질 검증에는 별도 API 사용료가 발생합니다.

### 전사 엔진만 재배포하기

일반 업데이트에서는 Gateway와 Worker를 GitHub의 같은 커밋 이미지로 함께 갱신합니다. `--worker-only`는 Gateway 계약이 바뀌지 않은 경우의 제한적인 운영·시험용 옵션입니다. 모델 갱신·중복 처리 정책이 변경된 릴리스는 두 리소스를 함께 갱신하세요. `preflight-cloud-run.sh`는 `update-cloud-run.sh --dry-run`과 같습니다.

### 저비용 운영 구성

공개 저장소의 표준 GitHub Actions 러너가 이미지를 빌드하고 GitHub Container Registry(GHCR)에 게시합니다. Cloud Run에는 그 이미지를 직접 배포합니다. 이 경로에는 프로젝트 소유 Artifact Registry나 Cloud Build 스테이징 버킷이 필요 없습니다. 롤백할 이미지는 GHCR에 보관합니다. Google Cloud 소스 빌드 배포나 Artifact Registry에 직접 업로드하는 방식으로 바꾸면 저장 비용이 다시 생길 수 있습니다. [GitHub Actions 요금](https://docs.github.com/en/billing/concepts/product-billing/github-actions) · [GHCR 요금](https://docs.github.com/en/billing/concepts/product-billing/github-packages)

실행 중인 원격 전사에는 로컬 체크아웃이 필요 없습니다. 나중에 배포나 관리를 할 때는 GitHub 저장소를 Cloud Shell에 다시 클론하고 기존 Google Cloud 설정과 Secret Manager 참조를 사용합니다.

서울의 기존 ChatGPT 연결을 유지하면서 전사만 아이오와로 옮기는 경우, Gateway의 리전과 OAuth URL은 유지하고 새 리전의 Worker로 명시적으로 전환합니다. 이후 일반 업데이트는 Gateway에 설정된 실제 Worker 리전을 자동으로 사용합니다. 이전 리전으로의 상태 조회가 필요 없고 이전·시험 Job에 실행 중인 작업이 없으면, Gateway에서 `SCRIBEFLOW_PREVIOUS_WORKER_REGION`을 제거한 뒤 그 Job들을 삭제합니다. 기존 Drive 전사문은 별도로 보관됩니다.

구형 Artifact Registry를 정리할 때는 현재 서비스·Job과 롤백 이미지가 GHCR에 있는지 먼저 확인합니다. Artifact Registry의 이미지 버전 목록이 비어 있어도 구성 파일과 저장 용량이 남을 수 있습니다. 구형 저장소에 보존할 것이 없다면 저장소를 삭제하고, 전용 프로젝트의 아래 점검에서 저장소와 버킷 목록이 비어 있는지 확인합니다. [Artifact Registry 저장소 삭제](https://docs.cloud.google.com/artifact-registry/docs/repositories/delete-repos)

```bash
export GCP_PROJECT='YOUR_EXISTING_PROJECT_ID'
gcloud run jobs list --project="$GCP_PROJECT"
gcloud artifacts repositories list --project="$GCP_PROJECT" --location=all
gcloud storage buckets list --project="$GCP_PROJECT"
gcloud firestore fields ttls list --project="$GCP_PROJECT" --database=scribeflow-oauth
```

비용은 결제 보고서에서 **SKU와 리전**으로 나눠 보고, 무료 할당량 할인은 남겨 둔 채 프로모션 크레딧만 제외해 확인합니다. Cloud Run 실행 시간과 외부 전송량에는 무료 한도가 있으며, ScribeFlow 자체에는 월간 전송 차단 한도가 없습니다. 새 음성을 전사할 때 OpenAI API 사용료는 별도로 발생합니다. Google Cloud 무료 체험은 자동으로 유료 전환되지 않으며, 전환 없이 체험이 끝나면 Cloud Run도 멈춥니다. [Cloud Run 요금](https://cloud.google.com/run/pricing) · [Cloud Billing 보고서](https://docs.cloud.google.com/billing/docs/how-to/reports) · [Google Cloud 무료 체험](https://cloud.google.com/signup-faqs) · [OpenAI API 요금](https://platform.openai.com/pricing)

### 전사 산출물·운영 참고

- **다른 리전 시험:** 기존 ChatGPT 연결을 유지하려면 별도 Cloud Run Job을 대상 리전에 만들고, 기존 전사 작업과 같은 이미지·실행 계정·Secret Manager 참조를 사용합니다. 짧은 합성 음성의 Drive ID로 시험 작업을 한 번 실행하고, TXT 생성과 작업 로그를 확인합니다. 미국 OpenAI 주소를 시험하려면 해당 API 프로젝트에서 사용 가능한지 확인한 뒤 시험 Job의 `OPENAI_BASE_URL`에 `https://us.api.openai.com/v1`을 설정합니다. 기존 Gateway를 전환하기 전에 결제 보고서에서 결과를 확인합니다.
- **전사 Job 리전 전환:** 의도적인 리전 이전 시 전체 설정을 적용하는 `deploy-cloud-run.sh --reconfigure`에서 `SCRIBEFLOW_REGION`은 기존 Gateway 리전, `SCRIBEFLOW_WORKER_REGION`은 새 전사 Job 리전으로 지정합니다. 새 리전에도 기존 Job과 같은 이름을 쓰고 Gateway 서비스 계정에 실행 권한을 부여합니다. 기존 Job의 상태 조회를 지원하려면 `SCRIBEFLOW_PREVIOUS_WORKER_REGION`에 이전 Job 리전을 지정하고 이전 Job을 보존합니다. 기존 Gateway만 업데이트할 때는 Gateway의 `SCRIBEFLOW_REGION` 환경변수를 새 **Job 리전**으로 지정하면 OAuth와 ChatGPT 연결 주소를 유지할 수 있습니다. 지역별 OpenAI 주소를 사용하는 경우 전사 Job에 `OPENAI_BASE_URL`도 설정합니다.
- **사용자 Google Drive:** 최종 산출물은 원본 음성과 같은 폴더의 TXT(기본값) 또는 Markdown(선택) **한 개**입니다. 다른 형식의 Google Docs·Sheets·Slides 문서로 복사하는 작업은 기존 Google Drive 플러그인에 맡깁니다. Cloud Run 작업용 원본 복사본, 무재인코딩 오디오 조각, 구간별 전사·감지 언어 캐시 및 manifest는 정상 완료 후 삭제를 시도합니다. 원격 Worker는 전체 분할 오디오를 동시에 보관하지 않고 한 조각씩 처리합니다. 원본은 작업 메모리에 들어가야 하며, 기본 Worker는 2 GiB 메모리, 작업은 최대 45분 제한을 사용합니다. Cloud Run Job이 종료되면 임시 파일시스템도 사라집니다. 전사 결과가 모두 비어 있으면 파일을 생성하지 않고 실패 상태로 알립니다.
- **Cloud Logging:** Google Cloud의 기본 `_Default` 로그 버킷 보관기간은 통상 30일입니다. `_Required` 버킷은 400일이며 임의로 단축할 수 없습니다. 로그에는 녹음 원문이나 API 키를 출력하지 않도록 설계했지만, 오류 유형과 작업 메타데이터는 남을 수 있습니다. 기존 보관 정책을 확인하지 않고 변경하지 마세요.

**읽기 전용 점검(아직 아무것도 정리하지 않음):**

```bash
export GCP_PROJECT='YOUR_EXISTING_PROJECT_ID'
bash scripts/audit-cloud-storage.sh
```

점검 결과에는 클라우드 프로젝트와 리소스 구성 정보가 포함될 수 있으므로 공개 채팅이나 공개 저장소에 원본 출력을 그대로 올리지 마세요. 이 스크립트는 모든 리전의 ScribeFlow Gateway·Worker 이미지와 현재 연결 대상, 이전 리전의 Job, 앱 전용 Registry와 정리 정책, 기본 로그 보관기간을 조회합니다. 프로젝트 ID 외에 리전을 다시 설정하거나 Artifact Registry API를 활성화할 필요가 없으며 삭제·설정 변경은 하지 않습니다.

**이미지 정리 정책은 관리자 선택사항:** `scripts/artifact-cleanup-policy.json`은 90일 지난 *태그 없는* 이미지를 대상으로 하되 이미지 패키지마다 최근 5개를 유지하는 보수적 예시입니다. 이 예시도 오래 실행 중인 이미지나 필요한 이전 버전을 삭제할 위험이 있으므로 **자동 적용되지 않습니다.** 먼저 실행 중인 두 Cloud Run 리소스의 정확한 이미지 다이제스트와 롤백 이미지를 대조하세요. 테스트만 설정하려면 아래 명령을 수동으로 실행할 수 있으나, `--dry-run`도 Registry에 테스트 정책을 **저장**한다는 점에 유의하세요. 이 명령은 실제 이미지를 삭제하지 않습니다.

```bash
gcloud artifacts repositories set-cleanup-policies scribeflow \
  --project="$GCP_PROJECT" --location=asia-northeast3 \
  --policy=scripts/artifact-cleanup-policy.json --dry-run
```

정책을 실제 적용해 자동 삭제하도록 전환하는 작업은 이번 개발 범위에 포함되지 않습니다. 기존 Registry 정책이 있는지 먼저 확인하고, 최소 1일 이상 dry-run 감사 로그를 확인한 뒤 결정하세요. Google 공식 [Artifact Registry 정책 안내](https://docs.cloud.google.com/artifact-registry/docs/repositories/cleanup-policy)와 [Cloud Logging 보관 정책](https://docs.cloud.google.com/logging/quotas)을 참고하세요.

| 현상 | 확인할 설정 |
| --- | --- |
| `redirect_uri_mismatch` | Google Cloud OAuth 웹 클라이언트의 **승인된 리디렉션 URI**가 `.../auth/callback`인지 |
| `invalid_target` / Resource mismatch | ChatGPT의 **서버 URL**이 `.../mcp`인지 (`.../auth/callback` 아님) |
| 연결 시 `403` | Cloud Run 서비스의 비공개 상태. `scripts/enable-mcp-access.sh` 실행 |
| Google 계정 권한 오류 | OAuth 테스트 사용자, 로그인 이메일, Cloud Run `SCRIBEFLOW_GOOGLE_EMAIL` 일치 여부 |
| 전사 작업 실패 | [Cloud Run Job 실행 로그](https://console.cloud.google.com/run/jobs)의 `scribeflow-transcribe` 확인 (토큰·비밀은 공유하지 않음) |
| 공유받은 폴더의 파일 검색 실패 | 연결한 Google 계정의 해당 파일 접근 권한 확인 |

## 완전 제거

**ScribeFlow 소유 리소스와 공용 사용자 자산을 구분합니다.** 기본 제거 스크립트는 **Google Cloud 프로젝트, 기존 OpenAI/rclone 키, 음성 원본·TXT·MD, GitHub 저장소를 보존**합니다. 전용 Google Cloud 프로젝트라면 마지막에 프로젝트 자체를 삭제하는 방식이 가장 깔끔합니다.

1. **ChatGPT**: Plugins에서 ScribeFlow 연결을 삭제합니다. Google 계정의 [제3자 앱 연결](https://myaccount.google.com/connections)에서도 ScribeFlow 접근 권한을 취소할 수 있습니다.
2. **Cloud Shell**: 저장소를 다시 클론하거나 기존 `~/ScribeFlow`에서 아래 스크립트를 실행합니다. 먼저 **dry-run으로 제거 대상 목록**을 확인합니다.

   진행 중인 전사가 끝난 뒤 제거하세요. 프로젝트 ID만 지정하면 모든 리전에서 실제 ScribeFlow 서비스·Job·Registry를 찾습니다. 이전 리전의 표준 Job이나 Gateway 제거 후 남은 Job도 포함됩니다. 앱 전용 서비스 계정과 데이터베이스를 공유하므로 일부 리전만 제거하는 명령이 아닙니다. 수동 연결한 별도 이름의 Job은 전용 Worker 계정을 사용하는 경우에만 제거합니다.

   ```bash
   export GCP_PROJECT='YOUR_PROJECT_ID'
   cd ~/ScribeFlow
   bash scripts/uninstall-cloud-run.sh
   # 검토 후 앱 전용 리소스 실제 삭제:
   bash scripts/uninstall-cloud-run.sh --apply
   ```

   기존 Worker에서 OpenAI Secret 참조를 확인해 앱 전용 접근 권한만 해제하며 키 자체는 보존합니다. 이미 Worker를 수동으로 삭제했다면 사용자 지정 Secret 이름을 `SCRIBEFLOW_OPENAI_SECRET`로 지정할 수 있습니다.

   제거 범위: `scribeflow-mcp` Cloud Run 서비스, `scribeflow-transcribe` Job, `scribeflow-oauth` Firestore 데이터베이스·OAuth 로그인 상태, `scribeflow` Artifact Registry 저장소와 이미지, `scribeflow-worker`·`scribeflow-gateway` 서비스 계정 및 관련 IAM, `scribeflow-google-client-secret`·`scribeflow-oauth-seed`·구형 앱 전용 인증 비밀. Firestore·이미지 삭제는 되돌릴 수 없습니다.

3. **Google Auth Platform**의 [클라이언트](https://console.cloud.google.com/auth/clients)에서 ScribeFlow용 OAuth **웹 클라이언트**를 삭제합니다. 브랜드·테스트 사용자 설정은 프로젝트에 남을 수 있으나, 공용 프로젝트라면 다른 앱을 위해 보존하세요.
4. **Secret Manager**: OpenAI API 키를 저장한 Secret과 이전 버전의 rclone 인증 비밀은 *ScribeFlow 전용이고 더 이상 필요하지 않은 경우에만* 수동으로 삭제합니다. 다른 서비스가 사용하는 기존 비밀이면 유지합니다. **OpenAI API 키도** 다른 용도가 없다면 [OpenAI 대시보드](https://platform.openai.com/api-keys)에서 별도로 폐기합니다. rclone 인증 설정도 다른 프로그램에서 사용한다면 유지합니다.
5. **과금·잔여 리소스**: [예산 알림](https://console.cloud.google.com/billing/budgets), Cloud Build 스테이징 스토리지, Cloud Logging 및 기타 Google Cloud 자원을 검토합니다. 공용 결제 계정·공용 Cloud Shell 설정은 자동 삭제하지 않습니다.
6. **Google Cloud 프로젝트 자체 폐기(선택):** 이 프로젝트에 ScribeFlow 외의 데이터나 서비스가 없다는 것을 확인한 뒤 [리소스 관리자](https://console.cloud.google.com/cloud-resource-manager)에서 **해당 프로젝트를 종료**하면 남은 프로젝트 소유 자원도 정리할 수 있습니다. 프로젝트 삭제는 광범위하게 파괴적이므로 앱 제거 스크립트가 자동 수행하지 않습니다. Google Cloud 결제 계정과 OpenAI 계정은 별도입니다.
7. **로컬 설치물**: 실행 중인 전사가 없을 때 `scribeflow auth remove`(macOS 전용), `uv tool uninstall scribeflow`, 소스 체크아웃·전용 캐시를 제거합니다. 공용 `rclone.conf`, uv 캐시, FFmpeg·Python과 **사용자 원본 오디오 및 완성된 TXT**는 자동 삭제하지 않습니다.

포트폴리오로 보존하려면 **GitHub의 공개 소스 저장소는 그대로 두세요.** GitHub 저장소는 Cloud Run 인스턴스·로컬 설치와 별개입니다.
