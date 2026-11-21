# ScribeFlow 원격 설치 · 사용 · 완전 제거

Google Cloud Run에 개인용 MCP 서버와 요청당 실행되는 음성 전사 Job을 배포합니다. ChatGPT에서 **Google OAuth**로 로그인하고 Google Drive 음성 파일 ID를 전달하면, OpenAI API가 음성을 전사하여 **원본과 같은 Drive 폴더에 TXT**를 만듭니다. 서버는 유휴 시 인스턴스가 0개로 줄어듭니다. **설정 후에는 맥북을 켜둘 필요도, 다른 PC에 ScribeFlow를 설치할 필요도 없습니다.**

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

## 2. Google Drive rclone 인증을 Secret Manager에 등록

관리용 컴퓨터에서 [rclone](https://rclone.org/drive/)을 설치한 뒤 `rclone config`로 Google Drive 연결을 만들고 원격 이름을 **`gdrive`**로 지정합니다. 이 단계는 관리자가 처음 한 번만 수행합니다. 다른 ChatGPT 기기에는 rclone이 필요 없습니다. 기존 `gdrive:` 연결이 있으면 그대로 사용할 수 있습니다.

`rclone config file`로 `rclone.conf`의 위치를 확인합니다. 인증 갱신 토큰이 포함되므로 **GitHub·채팅·로그에 업로드하지 마세요.** 여러 계정의 인증을 함께 담았다면 ScribeFlow용 `gdrive` 설정만 담은 별도 구성 파일을 사용하는 것이 좋습니다.

Cloud Shell의 **파일 업로드** 메뉴로 `rclone.conf`를 Cloud Shell 홈 디렉터리에 올린 뒤 아래처럼 Secret Manager에 저장합니다. 완료 후 **Cloud Shell의 일회용 복사본만** 지웁니다. 로컬의 공용 rclone 설정은 유지합니다.

```bash
gcloud secrets create scribeflow-rclone-config \
  --replication-policy=automatic --data-file="$HOME/rclone.conf" \
  --project="$GCP_PROJECT"
rm -f "$HOME/rclone.conf"
export SCRIBEFLOW_RCLONE_SECRET='scribeflow-rclone-config'
```

이미 Drive 인증이 다른 이름으로 Secret Manager에 저장되어 있다면 새로 만들 필요 없이 `SCRIBEFLOW_RCLONE_SECRET`에 **그 기존 이름**을 지정합니다.

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

Google 로그인에는 `openid`·`userinfo.email`만 요청합니다. **Google Drive 파일 접근 권한은 별도 rclone 설정으로 처리**합니다. 로그인 이메일을 바꾸려면 Cloud Run의 `SCRIBEFLOW_GOOGLE_EMAIL` 설정과 Google OAuth 테스트 사용자 설정을 함께 변경하고 재인증해야 합니다.

## 5. Cloud Run 배포 · ChatGPT 연결

**같은 Cloud Shell 세션**에서 `GCP_PROJECT`, `SCRIBEFLOW_OPENAI_SECRET`, `SCRIBEFLOW_RCLONE_SECRET`, `SCRIBEFLOW_GOOGLE_CLIENT_ID`, `SCRIBEFLOW_GOOGLE_EMAIL`이 모두 설정된 상태로 실행합니다.

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

## 6. 사용 · 시험 · 운영

- `transcribe_audio(file_id, keywords=None, prompt="", retry=False, language="auto")`: Google Drive 음성 파일 ID **한 개**를 받아 전사 작업을 시작하며 OpenAI API 사용료가 발생합니다. 필요한 경우 `keywords`에 최대 20개의 고유명사·전문용어를, `prompt`에 500자 이내의 짧은 녹음 맥락을 전달할 수 있습니다. 둘 다 생략 가능하며 기본 전사 동작은 기존과 같습니다. 실제 발언을 지어내는 프롬프트를 제공하지 마세요. **동일 파일 ID의 중복 전사 요청**은 Firestore에 원자적으로 예약해 기존 Cloud Run 작업을 재사용합니다. 기존 작업이 완료·실패한 경우 `retry=True`를 명시해야 새 작업을 만들 수 있습니다. 단, 이전 시도가 실행 여부를 확인할 수 없는 상태로 끝난 경우에는 2시간의 안전 유예시간 후에만 명시적 재시도가 가능합니다. 재시도는 API 사용료가 추가로 발생할 수 있으므로 반드시 기존 작업을 먼저 확인하세요.
- `language` 옵션: 자동 감지(`auto`)가 기본값입니다. 한국어 등 입력 언어를 아는 경우에는 `ko`, `en`과 같은 힌트를 지정할 수 있습니다. 감지 실패 시 추가 재전사나 자동 재인코딩은 수행하지 않습니다. 큰 다중 오디오 트랙은 일부 트랙 유실을 막기 위해 명시적으로 중단합니다.
- `retranscribe_audio(file_id, keywords=None, prompt="", language="auto")`: 선택한 녹음을 다시 전사해 기존 전사문을 교체합니다. TXT의 원본 SHA-256이 현재 오디오와 일치해야 합니다. 성공한 경우 동일한 Drive 파일과 링크에 새 내용이 저장됩니다. 이전 내용은 [Drive 버전 보관 정책](https://developers.google.com/workspace/drive/api/guides/manage-revisions)의 적용을 받습니다. Drive 갱신에는 `gdrive`에 활성 Google OAuth 연결이 필요합니다.
- `get_transcription_status(operation)`: 실행 상태(`running` / `completed` / `failed`)를 조회합니다. 상태 조회는 새 전사를 시작하지 않습니다.

**모델 변경(관리자용):** Cloud Run 콘솔에서 `scribeflow-transcribe` Job의 환경변수 `SCRIBEFLOW_TRANSCRIPTION_MODEL`에 해당 OpenAI 전사 API에서 사용할 모델 ID를 설정합니다. 기본값은 `gpt-transcribe`입니다. 공급자가 고정 스냅샷 ID를 제공하면 그 ID를 직접 설정할 수 있습니다. 다음 실행부터 적용되며 모델 변경만으로 기존 TXT를 갱신하지 않습니다. 품질을 확인한 뒤 ChatGPT에서 파일을 지정해 재전사를 요청하세요. 모델 ID와 스냅샷이 같은 전사본의 내부 변경 이력은 TXT에서 판별할 수 없습니다.

**Google Drive 파일 ID 검색:** Cloud Run Worker는 비공개 rclone 설정의 현재 OAuth 액세스 토큰을 이용해 Drive API에서 파일과 상위 폴더의 메타데이터만 읽어 빠르게 경로를 구합니다. rclone이 최종 파일 ID까지 확인한 경우에만 해당 경로를 사용합니다. 공유받은 폴더·폴더 범위 제한·인증 형태 때문에 조회가 불가능하면 기존의 재귀 검색으로 되돌아갑니다. Google Drive 녹음 자체를 사전 다운로드하거나 인증 비밀을 출력하지 않습니다. 지원 확장자는 `.flac`, `.mp3`, `.mp4`, `.mpeg`, `.mpga`, `.m4a`, `.ogg`, `.wav`, `.webm`이며 나머지는 자동 손실 변환하지 않습니다.

**중복 실행 잠금:** 기존 `scribeflow-oauth` Firestore 데이터베이스의 별도 `scribeflow_job_claims` 컬렉션에 원본 Drive 파일 ID의 SHA-256 해시, 시작 시각, Cloud Run 작업 ID만 저장합니다. 녹음·전사문·키워드·인증 비밀은 저장하지 않습니다. 별도의 Firestore 리소스를 만들지 않지만, 소량의 읽기·쓰기·저장 요금이 발생할 수 있습니다. 새로 만든 예약에는 7일 후 만료 시각(`expires_at`)이 저장됩니다. **Firestore의 자동 삭제는 TTL 정책을 별도로 활성화해야 실제 적용됩니다.** 정책 활성화 전에는 예약 기록이 삭제되지 않습니다.

**선택적 보관 정책 설정(관리자용):** ScribeFlow를 업데이트한 뒤 Cloud Shell에서 `GCP_PROJECT` 환경변수를 설정하고 아래 명령을 한 번 실행하면, **ScribeFlow 작업 예약 메타데이터만** 만료 후 자동 정리됩니다. 기존 OAuth 토큰, 녹음·TXT, Cloud Run Job은 대상이 아닙니다. Firestore TTL은 즉시 삭제를 보장하지 않으며 삭제 작업에 소액 요금이 발생할 수 있습니다.

```bash
bash scripts/configure-claim-ttl.sh
```

TTL이 적용되어 7일 지난 예약이 사라지면 동일 파일 ID의 새 요청도 Cloud Run 작업 하나를 생성할 수 있습니다. 일반 전사 요청은 기존 TXT를 보호합니다. Cloud Run 실행 자체에 비용이 발생할 수 있습니다. 이전 작업의 상태 조회가 불가능할 때에는 최소 2시간 후에 명시적 재시도 또는 재전사가 가능합니다.

ID는 `https://drive.google.com/file/d/FILE_ID/view`에서 확인합니다. **ScribeFlow MCP 자체는 Google Drive 파일명 검색 도구가 없으므로** ChatGPT가 “SSAFY 폴더의 녹음”만으로 찾으려면 별도 Google Drive 연결이나 파일 링크가 필요합니다. 이미 동명 TXT가 있으면 SHA-256 일치 시 `skipped_existing`, 미확인·불일치 시에도 덮어쓰지 않습니다.

**실전 검증 방법:** 원본 음성만 있고 동명 TXT가 없는 파일로 전사 요청 → `completed` 확인 → 원본 Drive 폴더의 **새 TXT 파일 ID·생성시각·본문** 확인. 기존 TXT를 시험 삼아 제거한다면 **휴지통으로 이동**해 복구 가능한 상태로 두세요. 실제 API 호출은 비용이 발생합니다.

**검증 사실(2026-11-13):** 공유받은 Drive 폴더의 M4A 약 0.84MB와 일반 Drive의 SSAFY M4A 약 68MB에서 기존 TXT를 휴지통으로 옮긴 뒤 새 TXT가 생성됐습니다. Linux CI의 Python 테스트 및 Docker 빌드도 통과했습니다. Windows 로컬 CLI 및 각 모바일 ChatGPT 앱은 별도 실기 검증 전입니다.

**기존 배포 업데이트:** `git switch main && git pull --ff-only`로 갱신한 뒤 아래 **읽기 전용 사전 점검**을 먼저 실행합니다. 이미 사용 중인 GCP 프로젝트, 구독 결제 상태, Cloud Run Job/MCP의 현재 이미지 참조, Firestore DB, Secret Manager의 활성 비밀, 최근 실행 내역을 확인합니다. 해당 명령은 서비스·비밀을 변경하거나 음성을 전사하지 않습니다.

```bash
cd ~/ScribeFlow
git switch main
git pull --ff-only
export GCP_PROJECT='YOUR_EXISTING_PROJECT_ID'

# 기존 Google OAuth Client ID / 로그인 이메일도 비공개 설정에서 복원
# export SCRIBEFLOW_GOOGLE_CLIENT_ID='...'
# export SCRIBEFLOW_GOOGLE_EMAIL='...'
bash scripts/preflight-cloud-run.sh
```

사전 점검은 **현재 클라우드 설정에 접근 가능한 계정에서만** 실행할 수 있습니다. 비밀값이나 출력 전체를 공개 저장소·채팅에 붙여넣지 마세요. 실패하면 오류를 확인한 다음 배포를 중단하세요. 이미 진행 중인 전사 Job이 있다면 완료된 후 배포를 진행해야 합니다.

사전 점검에 통과했다면 **5단계의 두 배포 스크립트**를 재실행합니다. 기존 Google OAuth 클라이언트·OpenAI 키·Drive 인증을 재사용합니다. 실행 중인 서비스의 이미지 주소만 바꿀 때는 Cloud Run의 Job과 서비스에 `gcloud run jobs update --image`와 `gcloud run services update --image`를 각각 사용하면 기존 URL과 접근 설정을 유지합니다. 배포 자체는 OpenAI 전사 API를 호출하지 않습니다.

**배포 후 확인:** `bash scripts/audit-cloud-storage.sh`로 현재 서비스와 Job의 배포 이미지·이전 이미지, 이미지 정리 정책, Logging 보관 상태를 읽기 전용으로 확인합니다. `gcloud firestore fields ttls list --collection-group=scribeflow_job_claims --database=scribeflow-oauth --project="$GCP_PROJECT"`로 예약 기록의 TTL 활성화 여부를 검사합니다. TTL이 미설정일 때만 관리자가 `bash scripts/configure-claim-ttl.sh`를 선택적으로 실행할 수 있습니다. Artifact Registry의 자동 삭제 정책은 실행 중인 이미지 다이제스트와 롤백 버전을 확인하기 전에는 **활성화하지 않습니다.**

**기능 검증:** `scripts/enable-mcp-access.sh`는 익명 MCP 요청의 401과 OAuth 메타데이터의 200을 확인합니다. 이는 유료 전사 API를 호출하지 않습니다. Google Drive의 실제 음성 한 건을 전사하는 품질 검증은 별도 API 사용료가 발생하므로, 최종 TXT가 없는 테스트 녹음과 정확한 기준문을 준비한 경우에만 실행합니다. 기존 녹음·TXT는 삭제하지 않고, 이미 TXT가 있는 파일은 기본적으로 건너뜁니다.

### 전사 엔진만 안전하게 재배포하기

MCP 도구 정의나 OAuth Gateway가 바뀌지 않은 **전사 엔진 변경만** 적용할 때는 기존 Gateway·OAuth·IAM·Secret Manager를 건드리지 않고 Cloud Run Job 이미지만 갱신할 수 있습니다. 이 스크립트는 검토된 코드가 `main`에 병합되어 있고 로컬 체크아웃이 깨끗한 `main` 브랜치일 때만 실행됩니다.

```bash
cd ~/ScribeFlow
git switch main
git pull --ff-only
export GCP_PROJECT='YOUR_EXISTING_PROJECT_ID'
bash scripts/deploy-worker-only.sh
```

이 명령은 GitHub에서 이미 빌드된 이미지를 배포합니다. 되돌릴 때는 이전 정상 이미지로 `gcloud run jobs update scribeflow-transcribe --image=PREVIOUS_IMAGE --project="$GCP_PROJECT" --region=asia-northeast3`를 실행할 수 있습니다.

**주의:** 원격 전문용어 힌트와 중복 실행 방지처럼 MCP Gateway의 도구 정의도 바뀐 릴리스는 이 스크립트만으로 적용할 수 없습니다. 처음 적용할 때는 위의 `scripts/deploy-cloud-run.sh`와 `scripts/enable-mcp-access.sh` 전체 절차가 필요합니다.

### 전사 산출물·Cloud 비용 점검

- **다른 리전 시험:** 기존 ChatGPT 연결을 유지하려면 별도 Cloud Run Job을 대상 리전에 만들고, 기존 전사 작업과 같은 이미지·실행 계정·Secret Manager 참조를 사용합니다. 짧은 합성 음성의 Drive ID로 시험 작업을 한 번 실행하고, TXT 생성과 작업 로그를 확인합니다. 미국 OpenAI 주소를 시험하려면 해당 API 프로젝트에서 사용 가능한지 확인한 뒤 시험 Job의 `OPENAI_BASE_URL`에 `https://us.api.openai.com/v1`을 설정합니다. 기존 Gateway를 전환하기 전에 결제 보고서에서 결과를 확인합니다.
- **전사 Job 리전 전환:** 배포 스크립트에서 `SCRIBEFLOW_REGION`은 기존 Gateway 리전, `SCRIBEFLOW_WORKER_REGION`은 새 전사 Job 리전으로 지정합니다. 새 리전에도 기존 Job과 같은 이름을 쓰고 Gateway 서비스 계정에 실행 권한을 부여합니다. 기존 Job의 상태 조회를 지원하려면 `SCRIBEFLOW_PREVIOUS_WORKER_REGION`에 이전 Job 리전을 지정하고 이전 Job을 보존합니다. 기존 Gateway만 업데이트할 때는 Gateway의 `SCRIBEFLOW_REGION` 환경변수를 새 **Job 리전**으로 지정하면 OAuth와 ChatGPT 연결 주소를 유지할 수 있습니다. 지역별 OpenAI 주소를 사용하는 경우 전사 Job에 `OPENAI_BASE_URL`도 설정합니다.
- **무료 체험과 비용 비교:** Google Cloud 결제 **보고서 → 그룹화 기준 SKU → 절감액**에서 프로모션 크레딧을 제외하고 시험 날짜의 Cloud Run 인터넷 전송료와 컴퓨팅 소계를 확인합니다. 월별 무료 할당량은 계속 적용됩니다. 새 SKU가 보고서에 반영되기 전까지 ₩0은 판정할 수 없습니다. 체험이 끝나면 유료 계정으로 전환하지 않은 프로젝트의 실행이 중지됩니다. [Cloud Run 요금](https://cloud.google.com/run/pricing), [Cloud Billing 보고서](https://docs.cloud.google.com/billing/docs/how-to/reports)
- **사용자 Google Drive:** 최종 산출물은 원본 음성과 같은 폴더의 TXT **한 개**입니다. Cloud Run 작업용 원본 복사본, 무재인코딩 오디오 조각, 구간별 전사·감지 언어 캐시 및 manifest는 정상 완료 후 삭제를 시도합니다. Cloud Run Job이 종료되면 임시 파일시스템도 사라집니다. 전사 결과가 모두 비어 있으면 TXT를 생성하지 않고 실패 상태로 알립니다.
- **작업 예약 Firestore:** 실제 오디오는 저장하지 않습니다. 파일 ID 해시·Cloud Run 작업 ID·시각 정도의 메타데이터만 남기며, 7일 만료 필드는 TTL 기능을 실제 활성화해야 삭제됩니다. 동일 파일에 대한 중복 과금 방지와 메타데이터 최소 보관 사이의 절충입니다.
- **Docker 이미지:** Cloud Build로 배포할 때 Artifact Registry에 이전 버전이 축적될 수 있습니다. 현재 실행 중인 Cloud Run Job과 MCP Gateway의 이미지 및 되돌리기에 필요한 이미지는 보존해야 합니다.
- **Cloud Logging:** Google Cloud의 기본 `_Default` 로그 버킷 보관기간은 통상 30일입니다. `_Required` 버킷은 400일이며 임의로 단축할 수 없습니다. 로그에는 녹음 원문이나 API 키를 출력하지 않도록 설계했지만, 오류 유형과 작업 메타데이터는 남을 수 있습니다. 기존 보관 정책을 확인하지 않고 변경하지 마세요.

**읽기 전용 점검(아직 아무것도 정리하지 않음):**

```bash
export GCP_PROJECT='YOUR_EXISTING_PROJECT_ID'
bash scripts/audit-cloud-storage.sh
```

점검 결과에는 클라우드 프로젝트와 리소스 구성 정보가 포함될 수 있으므로 공개 채팅이나 공개 저장소에 원본 출력을 그대로 올리지 마세요. 이 스크립트는 활성 Job·서비스 이미지, Registry 이미지 목록, 기존 정리 정책, 로그 버킷 보관기간만 조회하며 삭제·설정 변경은 하지 않습니다.

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
| 공유받은 폴더의 파일 검색 실패 | 최신 이미지와 `gdrive` 인증 계정의 해당 파일 접근 권한 확인 |

## 완전 제거

**ScribeFlow 소유 리소스와 공용 사용자 자산을 구분합니다.** 기본 제거 스크립트는 **Google Cloud 프로젝트, 기존 OpenAI/rclone 키, 음성 원본·TXT, GitHub 저장소를 보존**합니다. 전용 Google Cloud 프로젝트라면 마지막에 프로젝트 자체를 삭제하는 방식이 가장 깔끔합니다.

1. **ChatGPT**: Plugins에서 ScribeFlow 연결을 삭제합니다. Google 계정의 [제3자 앱 연결](https://myaccount.google.com/connections)에서도 ScribeFlow 접근 권한을 취소할 수 있습니다.
2. **Cloud Shell**: 저장소를 다시 클론하거나 기존 `~/ScribeFlow`에서 아래 스크립트를 실행합니다. 먼저 **dry-run으로 제거 대상 목록**을 확인합니다.

   ```bash
   export GCP_PROJECT='YOUR_PROJECT_ID'
   export SCRIBEFLOW_OPENAI_SECRET='YOUR_OPENAI_SECRET_NAME'
   export SCRIBEFLOW_RCLONE_SECRET='YOUR_RCLONE_SECRET_NAME'
   cd ~/ScribeFlow
   bash scripts/uninstall-cloud-run.sh
   # 검토 후 앱 전용 리소스 실제 삭제:
   bash scripts/uninstall-cloud-run.sh --apply
   ```

   제거 범위: `scribeflow-mcp` Cloud Run 서비스, `scribeflow-transcribe` Job, `scribeflow-oauth` Firestore 데이터베이스·OAuth 로그인 상태, `scribeflow` Artifact Registry 저장소와 이미지, `scribeflow-worker`·`scribeflow-gateway` 서비스 계정 및 관련 IAM, `scribeflow-google-client-secret`·`scribeflow-oauth-seed`·구형 앱 전용 인증 비밀. Firestore·이미지 삭제는 되돌릴 수 없습니다.

3. **Google Auth Platform**의 [클라이언트](https://console.cloud.google.com/auth/clients)에서 ScribeFlow용 OAuth **웹 클라이언트**를 삭제합니다. 브랜드·테스트 사용자 설정은 프로젝트에 남을 수 있으나, 공용 프로젝트라면 다른 앱을 위해 보존하세요.
4. **Secret Manager**: `SCRIBEFLOW_OPENAI_SECRET` 및 `SCRIBEFLOW_RCLONE_SECRET`가 *ScribeFlow 전용으로 새로 만든 비밀인 경우에만* 수동으로 삭제합니다. 다른 서비스가 사용하는 기존 비밀이면 유지합니다. **OpenAI API 키도** 다른 용도가 없다면 [OpenAI 대시보드](https://platform.openai.com/api-keys)에서 별도로 폐기합니다. rclone 인증 설정도 다른 프로그램에서 사용한다면 유지합니다.
5. **과금·잔여 리소스**: [예산 알림](https://console.cloud.google.com/billing/budgets), Cloud Build 스테이징 스토리지, Cloud Logging 및 기타 Google Cloud 자원을 검토합니다. 공용 결제 계정·공용 Cloud Shell 설정은 자동 삭제하지 않습니다.
6. **Google Cloud 프로젝트 자체 폐기(선택):** 이 프로젝트에 ScribeFlow 외의 데이터나 서비스가 없다는 것을 확인한 뒤 [리소스 관리자](https://console.cloud.google.com/cloud-resource-manager)에서 **해당 프로젝트를 종료**하면 남은 프로젝트 소유 자원도 정리할 수 있습니다. 프로젝트 삭제는 광범위하게 파괴적이므로 앱 제거 스크립트가 자동 수행하지 않습니다. Google Cloud 결제 계정과 OpenAI 계정은 별도입니다.
7. **로컬 설치물**: 실행 중인 전사가 없을 때 `scribeflow auth remove`(macOS 전용), `uv tool uninstall scribeflow`, 소스 체크아웃·전용 캐시를 제거합니다. 공용 `rclone.conf`, uv 캐시, FFmpeg·Python과 **사용자 원본 오디오 및 완성된 TXT**는 자동 삭제하지 않습니다.

포트폴리오로 보존하려면 **GitHub의 공개 소스 저장소는 그대로 두세요.** GitHub 저장소는 Cloud Run 인스턴스·로컬 설치와 별개입니다.
