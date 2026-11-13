# ScribeFlow

[MIT License](LICENSE) · [원격 설치 및 완전 제거](docs/cloud-run.md)

긴 한국어 녹음을 OpenAI `gpt-transcribe` 모델로 전사하는 오픈소스 프로젝트입니다. **로컬 CLI** 또는 **Google Cloud Run에 배포한 개인용 ChatGPT MCP 서버**로 사용할 수 있습니다. 최종 산출물은 원본 오디오 옆에 저장되는 같은 이름의 `.txt` 파일 하나입니다. AI가 요약·의견·화자 정보를 만들어 넣지 않으며, 출처 확인용 SHA-256 주석만 마지막에 추가합니다.

**어떤 방식을 선택해야 하나요?**

| 사용 방식 | 설치할 곳 | 이후 사용 |
| --- | --- | --- |
| **ChatGPT 원격 전사** | Google Cloud에 1회 배포하고 ChatGPT에 MCP 연결 | 다른 기기는 같은 ChatGPT 계정·워크스페이스에서 사용. 맥북은 꺼도 됨 |
| **로컬 CLI** | 전사할 컴퓨터에 1회 설치 | 해당 컴퓨터에서 녹음 파일을 직접 지정 |

ChatGPT 원격 전사가 목표라면 **[처음부터 배포하고, 나중에 완전히 제거하는 안내](docs/cloud-run.md)**부터 시작하세요. 별도 유료 인증 업체나 항상 켜두는 PC는 필요 없습니다. 단, Google Cloud 결제 및 OpenAI API 사용료는 별도이며, 원격 서버의 최초 연결에는 Google OAuth 인증이 필요합니다.

**동작 구조:** ChatGPT → Google OAuth가 적용된 Cloud Run MCP 서버 → 요청당 실행되는 Cloud Run Job → Google Drive에서 원본 다운로드 → OpenAI 음성인식 → 원본 폴더에 TXT 저장. 서버는 유휴 상태에서 0개 인스턴스로 축소됩니다.

실제 검증: 개인 Drive와 공유받은 Drive 폴더에서 각각 기존 TXT를 휴지통으로 옮긴 뒤, **원본 M4A만 남긴 상태로 새 TXT를 생성**했습니다(2026-11-13). 별도 설치한 컴퓨터·모바일의 ChatGPT 연결 자동 승계 여부는 환경별로 확인해야 합니다.

## 로컬 CLI 설치 (선택)

- [uv](https://docs.astral.sh/uv/), FFmpeg/FFprobe (Python 3.10+는 uv가 관리 가능)
- OpenAI API 키와 사용 가능한 API 크레딧
- Google Drive를 사용할 경우 [rclone](https://rclone.org/drive/)의 `gdrive:` 연결 (로컬 파일 처리에는 불필요)

```bash
uv tool install "git+https://github.com/LJY0317/ScribeFlow.git"
scribeflow auth set       # macOS 전용: API 키를 키체인에 저장
scribeflow auth status
```

macOS가 아닌 환경에서는 `auth set` 대신 `OPENAI_API_KEY` 환경변수를 설정하세요. FFmpeg 설치는 macOS(Homebrew `brew install ffmpeg`), Windows(`winget install Gyan.FFmpeg`), Linux(배포판 패키지 관리자) 등을 이용할 수 있습니다. Google Drive를 사용하려면 `rclone config`로 이름이 `gdrive`인 원격 저장소를 추가해야 합니다. 개발용 소스 설치는 `git clone` → `uv sync` → `uv run scribeflow ...` 순서입니다.

키는 채팅, 저장소 또는 명령 인수에 붙여넣지 마세요. macOS 이외에서는 `OPENAI_API_KEY` 환경변수를 사용합니다. `auth status`는 키 저장 여부만 검사합니다.

## 사용법

```bash
# 로컬 녹음 → 같은 폴더에 '녹음.txt'
scribeflow inspect '/path/to/녹음.m4a'
scribeflow transcribe '/path/to/녹음.m4a' --json

# 전문용어가 실제로 등장하는 녹음에만 인식 힌트를 선택적으로 제공
scribeflow transcribe '/path/to/다른녹음.m4a' --keyword 'GitLab' --keyword 'EC2'
```

### Google Drive

1. `rclone config`로 Google Drive 원격 저장소의 이름을 `gdrive`로 설정합니다.
2. 음성 파일의 **Google Drive URL, 파일 ID 또는 `gdrive:폴더/녹음.m4a` 경로**를 전달합니다.
3. ScribeFlow가 Drive에 있는 실제 파일을 식별하고, 로컬 임시 캐시에 다운로드한 뒤 전사합니다. 완료 후 `녹음.txt`를 원본과 같은 Drive 폴더에 업로드합니다.

```bash
# 큰 Drive에서 탐색 범위를 제한하려면 해당 폴더의 ID를 선택적으로 설정
export SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID='구글드라이브_폴더_ID'

# Drive 폴더의 아직 전사하지 않은 음성 파일 검색 (전사 API 호출 없음)
scribeflow scan gdrive: --recursive --json

# 파일 URL 입력 (일반 Google Drive 파일 보기 링크)
scribeflow paths 'https://drive.google.com/file/d/파일_ID/view'
scribeflow transcribe 'https://drive.google.com/file/d/파일_ID/view' --json

# 파일 ID로도 가능
scribeflow transcribe '파일_ID' --json
```

`SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID`는 선택 사항입니다. 설정하면 해당 폴더와 하위 파일에서만 검색하고, 설정하지 않으면 연결된 Drive의 기본 루트를 사용합니다. **파일 URL/ID를 처음 해석할 때 해당 범위의 파일 메타데이터를 조회**합니다. 넓은 Drive에서는 상위 폴더 ID를 지정하면 검색이 빨라집니다.

완료 결과 JSON에는 `status`, `source`, `transcript`, `model`, `url`(Drive인 경우)이 포함됩니다. 같은 폴더에 동명 TXT가 있으면 마지막 줄의 SHA-256을 원본 음성과 비교합니다. 일치하면 `skipped_existing`, 해시가 없으면 `unverified_existing`, 불일치하면 `source_mismatch`입니다. **어떤 경우에도 기존 TXT를 자동 덮어쓰거나 재전사하지 않습니다.** 미확인·불일치 결과는 종료 코드 1로 반환합니다. 재전사하려면 `--output`으로 같은 폴더의 다른 `.txt` 파일명을 지정하세요.

### ChatGPT 원격 MCP 사용

[Cloud Run 배포 안내](docs/cloud-run.md)를 완료하면 ChatGPT에서 `ScribeFlow` MCP를 선택하고 Drive **파일 링크 또는 파일 ID**를 지정해 전사할 수 있습니다. 서버가 제공하는 도구는 `transcribe_audio(file_id)`, `get_transcription_status(operation)` 두 개입니다. **ScribeFlow 자체에는 Drive 파일명 검색 도구가 없습니다.** ChatGPT가 폴더 이름만으로 파일을 찾게 하려면 Google Drive 연결 등 별도의 파일 검색 경로가 있어야 합니다. 원격 서비스는 로컬 맥북·CLI에 의존하지 않습니다.

## 원문 보존과 오디오 처리

- **본문은 음성에서 인식된 발언만 출력**합니다. 문장부호·문단 구분·공백만 정리하며 요약, 결론, AI가 만든 화자 정보, 가상 발언을 넣지 않습니다. 맨 끝에는 `<!-- ScribeFlow SHA256: 64자리해시 -->` 형식으로 원본 파일의 SHA-256만 한 줄 기록합니다. 본문에 포함된 이 주석은 음성 발언이 아닙니다.
- SHA-256은 파일명·Drive 파일 ID가 달라져도 **동일한 바이트의 음성 파일인지** 비교할 수 있습니다. Drive가 해시를 제공하면 원본 전체를 다시 내려받지 않고 검증합니다. 다만 파일 재인코딩 시 값이 달라지며, 해시 일치는 전사문 발언 내용의 정확성을 증명하지 않습니다.
- **원본 음성 보존:** 지원 형식이고 원본 크기가 25MB 이하이면 변환·분할 없이 그대로 OpenAI API에 전달합니다. 파일 크기가 초과할 때만 FFmpeg 스트림 복사(`-c:a copy`)로 원래 압축된 오디오를 유지하며 독립적으로 재생 가능한 조각을 만듭니다. 각 조각의 실제 크기를 API 호출 전에 검사하고, 크면 길이를 줄여 다시 분할합니다. 손실 재인코딩은 자동으로 하지 않습니다. 지원하지 않는 형식은 안내 후 중단합니다.
- 큰 파일은 가급적 침묵 지점에서 나누고, 말 중간에서 나누는 경우 짧게 겹치도록 합니다. 스트림 복사에서는 코덱 프레임 경계에 따라 정확한 절단 시각이 조금 달라질 수 있습니다. 그 경계에서 발언이 중복·누락될 가능성은 완전히 제거되지 않으며 ScribeFlow는 임의로 발언을 삭제하지 않습니다.
- 최종 사용자 폴더에는 원본 오디오와 같은 이름의 `.txt`만 추가됩니다. Drive 원본도 변경하지 않습니다.
- 전사 시 오디오 내용은 OpenAI API로 전송됩니다. 개인정보 및 녹음 활용 권한을 확인하세요.

## 임시 캐시와 재시도

GitHub에서 받은 소스 폴더에서 실행하는 경우 캐시는 **프로젝트 내부 `.scribeflow/cache/`**에 저장됩니다. 이 폴더는 Git 업로드 대상에서 제외됩니다. 별도로 패키지만 설치해 사용하는 경우에는 OS 표준 사용자 캐시 폴더를 사용합니다. 필요하면 `SCRIBEFLOW_CACHE_DIR`로 경로를 변경할 수 있습니다. 필요한 경우 원본 코덱을 보존한 분할본, 구간별 전사문, manifest 및 잠금 파일은 이 캐시에만 생성됩니다.

- **성공 후 즉시 제거**, 실패한 작업만 이어서 실행할 수 있도록 보관합니다.
- 실패 캐시는 7일 또는 전체 1GiB 한도 기준으로 오래된 작업부터 정리합니다. 활성 작업은 잠금으로 보호합니다.
- `scribeflow cache status`로 확인하고 `scribeflow cache clean`으로 정리합니다. 정리는 전사 시작 때도 수행합니다. 상시 백그라운드 서비스는 없습니다.
- 실패 캐시를 삭제하거나 API에 이미 도달한 요청이 중단되면 재시도 시 비용이 추가 발생할 수 있습니다.

## 테스트

```bash
uv run python -m unittest discover -s tests -v
```

현재 별도의 웹 UI, 화자 자동 식별 또는 AI 요약 모델은 포함하지 않습니다.

### 원격 전사 (선택 기능)

로컬 설치 없이 다른 컴퓨터의 ChatGPT에서 전사 요청을 실행하려면
[Cloud Run + 인증된 MCP 배포 안내](docs/cloud-run.md)를 참고하세요.
기존 로컬 CLI는 변경 없이 사용할 수 있습니다. 클라우드 작업은
Google Cloud 프로젝트·결제·Secret Manager·OAuth 설정을 별도로 필요로
하며 기본적으로 배포되지 않습니다.

## 제거 및 잔여 파일

**원격 사용:** [클라우드·OAuth·Google 프로젝트·API 키 제거 가이드](docs/cloud-run.md#완전-제거)를 참고하세요. GitHub 저장소·사용자 원본 음성·완성된 TXT는 명시적으로 삭제하지 않는 한 보존합니다.

**로컬 CLI:** 패키지 설치 방식이라면 다음 순서로 제거합니다.

```bash
scribeflow auth remove   # macOS 키체인에 저장한 ScribeFlow 전용 키만 제거
uv tool uninstall scribeflow
```

소스 폴더에서 사용했다면 전사가 끝난 뒤 해당 폴더 전체(`.venv/`, `.scribeflow/`, `dist/` 포함)를 휴지통으로 보내면 됩니다. 구형 macOS 캐시 `~/Library/Caches/ScribeFlow/` 및 패키지 설치 캐시는 별도 확인하세요.

`SCRIBEFLOW_CACHE_DIR`을 별도 지정했다면 그 경로도 확인하세요. 공용 `uv` 다운로드 캐시, `rclone` 인증 설정, FFmpeg, Python 등은 다른 프로그램에서도 사용하므로 제거 스크립트가 건드리지 않습니다. 원본 음성과 완성된 TXT는 사용자 폴더나 Google Drive에 그대로 남습니다.
