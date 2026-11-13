# Google Cloud ScribeFlow — ChatGPT 연결 1개

Google Cloud Run의 MCP 서버가 전사 요청을 받으면 Cloud Run Job이 **선택한 Drive 음성 파일 한 개**를 처리합니다. 기존 OpenAI API와 rclone Drive 인증을 재사용해 결과 TXT를 원본 폴더에 저장합니다. 추가 인증 업체나 다른 컴퓨터에 설치할 프로그램은 없습니다.

## 첫 설치

기존 Google Cloud 프로젝트에서 `gcloud` 인증된 **Cloud Shell**을 사용합니다.

```bash
cd ~/ScribeFlow
export GCP_PROJECT='YOUR_PROJECT_ID'
export SCRIBEFLOW_OPENAI_SECRET='YOUR_EXISTING_OPENAI_SECRET_NAME'
export SCRIBEFLOW_RCLONE_SECRET='YOUR_EXISTING_RCLONE_SECRET_NAME'
```

1. **Google OAuth용 주소 확인:** Cloud Shell에서 아래 명령으로 서버 주소를 확인합니다. 아직 배포하지 않아도 주소를 알 수 있습니다.

   ```bash
   echo "https://scribeflow-mcp-$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)').asia-northeast3.run.app"
   ```

2. [Google Auth Platform](https://console.cloud.google.com/auth/overview)에서 **브랜딩/대상(Audience)** 을 설정합니다. 개인 Google 계정은 `External` + 테스트 사용자로 본인의 Google 계정을 추가할 수 있습니다. **Clients → Create Client → Web application**을 선택해 `ScribeFlow` 클라이언트를 생성합니다.

   - Authorized JavaScript origins: 위에서 확인한 `https://...run.app` 주소
   - Authorized redirect URI: 같은 주소 뒤에 `/auth/callback` 추가
   - OAuth 요청 권한: `openid`, `userinfo.email`만 사용합니다. ScribeFlow는 여기서 Google Drive 접근을 요청하지 않습니다.

3. 발급된 **Client ID**를 다음 환경변수에 입력합니다. 사용하는 **본인 Google 계정 이메일**도 입력합니다. 이 계정 외에는 유료 전사 도구를 호출할 수 없습니다.

   ```bash
   export SCRIBEFLOW_GOOGLE_CLIENT_ID='CLIENT_ID.apps.googleusercontent.com'
   export SCRIBEFLOW_GOOGLE_EMAIL='YOUR_GOOGLE_LOGIN_EMAIL'
   ```

4. 발급된 **Client Secret**은 채팅이나 코드가 아니라 Google Cloud Secret Manager에 저장합니다. 아래 명령어의 `read` 프롬프트에만 비밀값을 붙여넣으세요. 입력한 값은 화면에 표시되지 않습니다. 기존에 만든 OpenAI/rclone 비밀은 건드리지 않습니다.

   ```bash
   read -r -s -p 'Google OAuth Client Secret: ' GOOGLE_CLIENT_SECRET; echo
   printf '%s' "$GOOGLE_CLIENT_SECRET" | gcloud secrets create scribeflow-google-client-secret \
     --replication-policy=automatic --data-file=- --project="$GCP_PROJECT"
   unset GOOGLE_CLIENT_SECRET
   ```

5. 한 번만 배포합니다. 스크립트가 별도의 OAuth 키를 Secret Manager에 생성하고, ScribeFlow 전용 Firestore 데이터베이스 `scribeflow-oauth`를 만들며, **외부 접근이 막힌 상태**로 Cloud Run 서버와 Job을 배포합니다.

   ```bash
   bash scripts/deploy-cloud-run.sh
   bash scripts/enable-mcp-access.sh
   ```

   마지막 명령은 익명 `/mcp` 요청이 **HTTP 401**, OAuth 검색 메타데이터가 **HTTP 200**인지 검사합니다. 실패하면 서버를 다시 비공개로 전환합니다.

## ChatGPT에서 사용

ChatGPT → Plugins → 사용자 지정 MCP 서버 추가에서 `https://...run.app/mcp`를 연결하고 Google OAuth 로그인합니다. **별도의 ScribeFlow 플러그인 패키지는 만들 필요가 없습니다.** 사용자에게 제공되는 도구는 아래 두 개뿐입니다.

- `transcribe_audio(file_id)`: 요청한 Drive 녹음 파일 하나 전사 시작 (OpenAI API 요금 발생)
- `get_transcription_status(operation)`: 진행·완료·실패 상태 확인 (추가 전사 없음)

이후 다른 PC나 Mac, 모바일에서도 같은 ChatGPT 계정으로 사용할 수 있으며 Python, rclone, FFmpeg 설치는 필요 없습니다. ScribeFlow 서버는 요청 없을 때 0개 인스턴스로 축소됩니다. 파일 ID 탐색은 연결된 `gdrive:`의 루트 전체를 대상으로 하지만, 공유 드라이브 파일 일부는 연결 범위에 따라 검색되지 않을 수 있습니다.

## 제거

ChatGPT의 ScribeFlow 연결을 해제하고, Google Auth Platform의 `ScribeFlow` **OAuth Web Client**를 삭제합니다. Cloud Shell에서는 먼저 제거 대상만 확인합니다.

```bash
bash scripts/uninstall-cloud-run.sh
# 실제 클라우드 리소스 삭제를 명시적으로 승인할 때만:
bash scripts/uninstall-cloud-run.sh --apply
```

이는 전용 Cloud Run Service·Job, Firestore OAuth DB, 이미지 저장소, 전용 서비스 계정, ScribeFlow OAuth 비밀만 정리합니다. **Google Cloud 프로젝트, 사용자 녹음·TXT, 기존 OpenAI API 키와 rclone 인증 비밀은 보존합니다.** 삭제는 되돌릴 수 없으므로 검토 후 실행하세요.

Google 로그인 OAuth는 FastMCP의 `GoogleProvider` 오픈소스 구현을 사용합니다. Cloud Run 인스턴스 재시작 후 인증 상태를 보존하기 위해 Google Firestore에 **암호화된** OAuth 상태를 저장합니다. Google Cloud 리소스/Firestore와 OpenAI API에는 비용이 발생할 수 있으며 Cloud Run 예산 설정만으로 다른 서비스의 사용료가 제한되지는 않습니다. 실제 로그인 및 전사 성공은 클라우드 배포 후 검증해야 합니다.
