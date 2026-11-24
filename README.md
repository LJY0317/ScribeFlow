# ScribeFlow

[MIT License](LICENSE) · [원격 설치 및 완전 제거](docs/cloud-run.md) · [저비용 클라우드 운영](docs/cloud-run.md#저비용-운영-구성)

다양한 언어의 긴 녹음을 OpenAI `gpt-transcribe` 모델로 전사하는 오픈소스 프로젝트입니다. **로컬 CLI** 또는 **Google Cloud Run에 배포한 개인용 ChatGPT MCP 서버**로 사용할 수 있습니다. 최종 산출물은 원본 오디오 옆에 저장되는 같은 이름의 `.txt`(기본값) 또는 `.md` 파일 하나입니다. 전사문 끝에는 원본과 전사 작업의 정보를 기록합니다.

**어떤 방식을 선택해야 하나요?**

| 사용 방식 | 설치할 곳 | 이후 사용 |
| --- | --- | --- |
| **ChatGPT 원격 전사** | Google Cloud에 1회 배포하고 ChatGPT에 MCP 연결 | 다른 기기는 같은 ChatGPT 계정·워크스페이스에서 사용. 맥북은 꺼도 됨 |
| **로컬 CLI** | 전사할 컴퓨터에 1회 설치 | 해당 컴퓨터에서 녹음 파일을 직접 지정 |

ChatGPT 원격 전사가 목표라면 **[처음부터 배포하고, 나중에 완전히 제거하는 안내](docs/cloud-run.md)**부터 시작하세요. 별도 유료 인증 업체나 항상 켜두는 PC는 필요 없습니다. 단, Google Cloud 결제 및 OpenAI API 사용료는 별도이며, 원격 서버의 최초 연결에는 Google OAuth 인증이 필요합니다.

**동작 구조:** ChatGPT → Google OAuth가 적용된 Cloud Run MCP 서버 → 요청당 실행되는 Cloud Run Job → Google Drive API로 원본·기존 전사문 확인 → 필요한 경우 OpenAI 음성인식 → Drive API로 TXT 또는 Markdown 저장·갱신. 서버는 유휴 상태에서 0개 인스턴스로 축소됩니다. 로컬 CLI의 Drive 전사도 같은 파일 ID 기반 처리 코드와 모델 갱신 정책을 사용합니다.

실제 검증: 개인 Drive와 공유받은 Drive 폴더에서 각각 기존 TXT를 휴지통으로 옮긴 뒤, **원본 M4A만 남긴 상태로 새 TXT를 생성**했습니다(2026-11-13). 별도 설치한 컴퓨터·모바일의 ChatGPT 연결 자동 승계 여부는 환경별로 확인해야 합니다.

## 로컬 CLI 설치 (선택)

- [uv](https://docs.astral.sh/uv/), FFmpeg/FFprobe (Python 3.10+는 uv가 관리 가능)
- OpenAI API 키와 사용 가능한 API 크레딧
- Google Drive를 사용할 경우 [rclone](https://rclone.org/drive/)의 Google OAuth `gdrive:` 연결 (로컬 파일 처리에는 불필요)

```bash
uv tool install "git+https://github.com/LJY0317/ScribeFlow.git"
scribeflow auth set       # macOS 전용: API 키를 키체인에 저장
scribeflow auth status
```

macOS가 아닌 환경에서는 `auth set` 대신 `OPENAI_API_KEY` 환경변수를 설정하세요. FFmpeg 설치는 macOS(Homebrew `brew install ffmpeg`), Windows(`winget install Gyan.FFmpeg`), Linux(배포판 패키지 관리자) 등을 이용할 수 있습니다. 로컬 CLI에서 Google Drive 파일을 직접 처리할 때만 `rclone config`로 이름이 `gdrive`인 원격 저장소를 추가하세요. 로컬 파일과 ChatGPT 원격 MCP에는 rclone이 필요 없습니다. 개발용 소스 설치는 `git clone` → `uv sync` → `uv run scribeflow ...` 순서입니다.

키는 채팅, 저장소 또는 명령 인수에 붙여넣지 마세요. macOS 이외에서는 `OPENAI_API_KEY` 환경변수를 사용합니다. `auth status`는 키 저장 여부만 검사합니다.

## 사용법

```bash
# 로컬 녹음 → 같은 폴더에 '녹음.txt'
scribeflow inspect '/path/to/녹음.m4a'
scribeflow transcribe '/path/to/녹음.m4a' --json

# 전문용어가 실제로 등장하는 녹음에만 인식 힌트를 선택적으로 제공
scribeflow transcribe '/path/to/다른녹음.m4a' --keyword 'GitLab' --keyword 'EC2'
# 기본적으로 자동 감지; 한국어가 확실할 때는 언어 힌트 지정
scribeflow transcribe '/path/to/한국어녹음.m4a' --language ko
# 원본 발언을 Markdown(.md)으로 직접 저장
scribeflow transcribe '/path/to/한국어녹음.m4a' --format md
```

### Google Drive

1. `rclone config`로 Google Drive 원격 저장소의 이름을 `gdrive`로 설정합니다.
2. 음성 파일의 **Google Drive URL, 파일 ID 또는 `gdrive:폴더/녹음.m4a` 경로**를 전달합니다.
3. URL·ID는 Drive API에 직접 전달합니다. rclone은 로컬 OAuth 갱신과 경로·폴더 탐색에만 사용합니다. 원본 다운로드, 기존 전사문 확인, 저장·갱신은 원격 MCP와 동일한 Drive API 코드가 파일 ID로 수행합니다. 별도의 rclone 파일 전송 fallback은 없습니다.

```bash
# 큰 Drive에서 탐색 범위를 제한하려면 해당 폴더의 ID를 선택적으로 설정
export SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID='구글드라이브_폴더_ID'

# Drive 폴더의 아직 전사하지 않은 음성 파일 검색 (전사 API 호출 없음)
scribeflow scan gdrive: --recursive --json

# 기존 TXT의 기록된 전사 모델과 각 원본 Drive 파일 ID 조회
scribeflow scan 'gdrive:폴더' --recursive --models --limit 1000 --json

# 파일 URL 입력 (일반 Google Drive 파일 보기 링크)
scribeflow paths 'https://drive.google.com/file/d/파일_ID/view'
scribeflow transcribe 'https://drive.google.com/file/d/파일_ID/view' --json

# 파일 ID로도 가능
scribeflow transcribe '파일_ID' --json
```

`SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID`는 선택 사항입니다. 설정하면 해당 폴더와 하위 파일에서만 검색하고, 설정하지 않으면 연결된 Drive의 기본 루트를 사용합니다. 범위가 지정되지 않은 파일 URL·ID 전사에는 전체 Drive 탐색이 필요 없습니다. `paths`, `scan`, 경로 입력 또는 범위를 지정한 전사에서는 해당 범위의 경로를 확인합니다.

전사 결과의 JSON에는 `status`, `source`, `transcript`, `model`, 실제 Drive 파일 ID·URL이 포함됩니다. ID·URL을 직접 입력했거나 다른 폴더의 파일을 갱신해 경로를 알 수 없으면 `transcript=null`과 실제 Drive ID·URL을 반환합니다. 같은 폴더의 다른 이름을 명시하는 `--output`도 지원합니다. 모델은 `SCRIBEFLOW_TRANSCRIPTION_MODEL`로 지정할 수 있으며 기본값은 `gpt-transcribe`입니다.

### 기존 전사문과 모델 갱신 정책 (CLI·MCP 공통)

- 같은 원본 SHA-256의 TXT·MD가 없으면 신규 전사합니다. 요청한 이름이 다른 원본의 파일이거나 출처를 검증할 수 없으면 덮어쓰지 않습니다.
- 기존 전사문 모델이 현재 모델과 같거나, 성능 개선 관계를 알 수 없거나, 더 전문적인 모델이면 `skipped_existing`으로 재사용합니다. 같은 원본의 다른 형식에 재사용 가능한 전사문이 있으면 추가 전사하지 않습니다.
- 현재 설정이 `gpt-transcribe`이고 기존 모델이 `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` 또는 명시된 mini 스냅샷인 경우에는 사용자가 선택한 음성을 새 모델로 전사해 갱신합니다(`upgraded`, `previous_model`과 `model` 반환). [OpenAI의 일반 파일 전사 권장 모델](https://developers.openai.com/api/docs/guides/speech-to-text)에 근거한 명시적 전환이며, 모든 녹음에서 품질이 개선된다고 보장하지 않습니다. 파일 ID·위치·형식은 유지합니다. 유료 전사에 실패하면 기존 내용을 보존합니다.
- 기록된 본문 SHA-256과 실제 본문이 일치할 때만 자동 갱신합니다. 본문 해시가 없거나 변경된 전사문은 기존 내용을 보존하며, 사용자가 직접 재전사를 요청할 때만 갱신합니다. Drive의 일반 버전 기록 정책을 사용하고 별도 영구 백업은 만들지 않습니다. 같은 모델 ID의 비공개 내부 변경이나 알려지지 않은 모델 차이를 추측하지 않습니다.
- `--replace-existing` 또는 `retranscribe_audio`는 원본 SHA-256이 확인된 기존 파일을 명시적으로 재전사합니다. 기존 파일의 형식에 맞추어 요청해야 합니다. 전체 Drive를 자동으로 순회하거나 일괄 갱신하지 않습니다.

`scan --models`는 기존 TXT·MD 끝부분을 읽고 `transcribed`에 각 원본의 Drive ID, 전사문 ID·형식, 해시 검증 상태, 공급자·모델 ID·스냅샷·전사 시각을 반환합니다. `model_counts`에서 모델별 개수를 확인할 수 있습니다. 두 형식이 모두 있으면 각각 조회하며, 해시만 있는 구형 전사문은 `model`은 `null`입니다. `--limit`은 반환 목록의 크기를 제한하며 개수는 조회 범위 전체를 반영합니다. 에이전트는 `upgrade_candidate=true`인 원본 ID를 골라 갱신 후보를 찾을 수 있습니다. 이 플래그는 모델 변경 후보를 표시합니다. 본문 해시 미확인·불일치 시 명시적 재전사가 필요합니다. 조회 결과는 표준 출력에만 표시됩니다.

### ChatGPT 원격 MCP 사용

[Cloud Run 배포 안내](docs/cloud-run.md)를 완료하면 ChatGPT에서 `ScribeFlow` MCP를 선택하고 Drive **파일 링크 또는 파일 ID**를 전달합니다. 원격 MCP는 연결된 Google 계정의 OAuth 권한으로 Drive API에 직접 접근합니다. 서버 측 rclone 설치나 별도 Drive 인증은 필요 없습니다. 기존 원격 설치를 업데이트할 때는 [이미지 갱신 절차](docs/cloud-run.md#6-사용--시험--운영)를 사용하면 기존 Google Cloud 설정이 유지됩니다.

- `transcribe_audio`는 새 녹음을 전사합니다. 기본 `output_format="txt"`이며, `output_format="md"`를 선택하면 Markdown으로 저장합니다. 같은 파일의 실행 중 작업은 재사용합니다. 완료 후 새로운 요청은 현재 Drive 상태와 Worker 모델을 다시 확인합니다. 원본 SHA-256이 일치하는 기존 TXT·MD는 위 모델 갱신 정책에 따라 재사용하거나 기존 ID에 갱신합니다. 실패한 작업 등을 다시 시도할 때 `retry=True`를 사용할 수 있습니다.
- `retranscribe_audio`는 선택한 녹음을 현재 설정 모델로 다시 전사하고, 원본 SHA-256이 일치하는 기존 TXT 또는 선택한 Markdown(`output_format="md"`)의 내용만 갱신합니다. 같은 Drive 파일 ID와 링크가 유지됩니다. 새 전사에 OpenAI API 사용료가 발생합니다.
- 기존 TXT를 다른 Google Drive 폴더로 이동한 경우에는 `retranscribe_audio(file_id="원본_음성_ID", transcript_file_id="기존_TXT_ID")`로 **기존 TXT 파일을 직접 지정**할 수 있습니다. 원본 SHA-256과 기존 TXT 말꼬리의 SHA-256이 일치해야 하며, 일치하지 않으면 전사 API를 호출하지 않습니다. 갱신 후 TXT의 파일 ID와 현재 폴더는 그대로 유지됩니다. `transcript_file_id`를 생략하면 원본 폴더를 먼저 확인합니다.
- 원본 폴더에 선택한 형식의 동명 전사문이 없으면 Drive에서 동일 파일명과 원본 SHA-256이 일치하는 후보 한 개만 갱신합니다. 여러 후보 또는 다른 이름의 전사문은 파일 ID로 직접 지정하세요. CLI에서도 `--replace-existing --transcript-file-id ID --format md` (TXT는 `--format txt`)로 기존 파일을 지정할 수 있습니다.
- `get_transcription_status`는 실행 상태와 결과를 조회합니다. 완료 시 실제 형식·Drive 파일 ID·URL·모델, 실패 시 오류 코드가 제공됩니다(결과 기록이 없는 이전 작업 등은 상태만 제공). 결과 ID를 기존 Google Drive 플러그인에 전달해 읽거나 변환할 수 있습니다.

**형식 변환은 재전사가 아닙니다.** 이미 만들어진 TXT를 Markdown·Google Docs·Sheets·Slides로 옮길 때에는 새 전사 요청을 하지 말고 ChatGPT의 기존 Google Drive 플러그인에서 원문을 읽고 결과 문서를 만드세요. 신규 전사의 `output_format`은 저장 형식을 선택하고, 재전사에서는 기존 파일의 형식을 지정합니다. 형식 변환만을 위해 재전사를 호출하지 마세요. 직접 선택한 음성의 일반 전사 요청에도 위 모델 갱신 정책이 적용되며, 같은 모델로 다시 전사할 때는 명시적 재전사를 사용하세요.

## 원문 보존과 오디오 처리

- **본문에는 인식된 발언과 원본 오디오의 시간 범위를 출력**합니다. 문단 구분과 공백을 정리합니다. TXT·MD 끝에는 원본 파일명, 음성 길이, 원본 크기(바이트), 감지 언어 목록, API 공급자, 요청 모델 ID, 공개 스냅샷, UTC 전사 완료 시각, ScribeFlow 버전을 기록합니다. `gpt-transcribe`의 스냅샷은 `미공개`로 표시합니다. 감지 언어는 API가 보고한 언어 코드를 구간 순서로 모아 중복을 제거하며, 보고된 코드가 없으면 `확인 불가`로 표시합니다. 원본의 SHA-256은 기존과 같은 형식으로 마지막 줄에 기록합니다. 본문 SHA-256도 메타데이터에 기록해 이후 수동 편집 여부를 확인합니다.
- SHA-256은 파일명·Drive 파일 ID가 달라져도 **동일한 바이트의 음성 파일인지** 비교할 수 있습니다. Drive가 해시를 제공하면 원본 전체를 다시 내려받지 않고 검증합니다. 다만 파일 재인코딩 시 값이 달라지며, 해시 일치는 전사문 발언 내용의 정확성을 증명하지 않습니다.
- **지원 입력 형식:** `.flac`, `.mp3`, `.mp4`, `.mpeg`, `.mpga`, `.m4a`, `.ogg`, `.wav`, `.webm` (대소문자 구분 없음). Google Drive 파일 스캔·파일 ID 처리·전사 엔진이 같은 목록을 사용합니다. `.aac`, `.qta`, `.opus`, `.wma` 등은 자동으로 손실 변환하지 않고 명확히 거부합니다.
- **원본 음성 보존:** 지원 형식이고 원본 크기가 25MB 이하이면 변환·분할 없이 그대로 OpenAI API에 전달합니다. 파일 크기가 초과할 때만 FFmpeg 스트림 복사(`-c:a copy`)로 원래 압축된 오디오를 유지하며 독립적으로 재생 가능한 조각을 만듭니다. 각 조각의 실제 크기를 API 호출 전에 검사하고, 크면 길이를 줄여 다시 분할합니다. 손실 재인코딩은 자동으로 하지 않습니다. 지원하지 않는 형식은 안내 후 중단합니다.
- 한 번에 전사하면 본문 앞에 `[00:00:00–00:08:24]`처럼 녹음 전체의 범위를 표시합니다. 분할해서 전사하면 발언이 있는 각 조각의 본문 앞에 원본 기준 범위를 표시합니다.
- 큰 파일은 가급적 침묵 지점에서 나누고, 말 중간에서 나누는 경우 짧게 겹치도록 합니다. 코덱 프레임 경계에 따라 실제 절단 시각이 표시된 범위와 약간 다를 수 있습니다. 분할 경계에서는 발언이 중복되거나 누락될 수 있습니다.
- 기본 언어는 자동 감지(`auto`)입니다. 한국어 등 입력 언어가 확실하면 CLI `--language ko` 또는 MCP `language="ko"`로 힌트를 줄 수 있습니다. 자동 감지 실패를 이유로 한국어 강제 재전사나 음질 변환을 수행하지 않습니다.
- 25MB 이하의 다중 오디오 트랙 파일은 원본 그대로 전달합니다. 분할이 필요한 다중 트랙 파일은 일부 트랙을 조용히 누락시키지 않도록 명시적으로 중단합니다.
- 최종 사용자 폴더에는 원본 오디오와 같은 이름의 `.txt`(기본) 또는 `.md`(선택) 한 개만 추가됩니다. Markdown은 구간 표시를 `##` 제목으로 표현하며 발언 내용은 변경하지 않습니다. Drive 원본도 변경하지 않습니다.
- 전사 시 오디오 내용은 OpenAI API로 전송됩니다. 개인정보 및 녹음 활용 권한을 확인하세요.

## 임시 캐시와 재시도

GitHub에서 받은 소스 폴더에서 실행하는 경우 캐시는 **프로젝트 내부 `.scribeflow/cache/`**에 저장됩니다. 이 폴더는 Git 업로드 대상에서 제외됩니다. 별도로 패키지만 설치해 사용하는 경우에는 OS 표준 사용자 캐시 폴더를 사용합니다. 필요하면 `SCRIBEFLOW_CACHE_DIR`로 경로를 변경할 수 있습니다. 필요한 경우 원본 코덱을 보존한 분할본, 구간별 전사문, manifest 및 잠금 파일은 이 캐시에만 생성됩니다.

- **성공한 전사는 최종 전사문만 게시하고 중간 작업 파일을 즉시 제거합니다.** 큰 녹음은 각 오디오 구간의 전사문이 저장되는 즉시 해당 오디오 조각도 제거합니다. Google Drive에는 원본 녹음과 최종 전사문만 남으며, 인식된 발언이 없으면 빈 전사문을 게시하지 않습니다.
- 로컬 CLI에서 **실패한 전사 캐시의 기본 보존 기간은 24시간**, 전체 **256MiB는 소프트 용량 예산**입니다. 만료되거나 초과해도 다음 전사 또는 수동 정리 시에만 정리되므로 실시간 절대 상한은 아닙니다. 활성 작업은 잠금으로 보호합니다. 로컬 Drive 다운로드는 정상 종료 즉시 제거하며, 비정상 종료 후 남은 다운로드는 다음 캐시 정리에서 제거하고 용량 집계에 포함합니다.
- 실패 시에도 중간 파일을 즉시 폐기하려면 `SCRIBEFLOW_FAILED_CACHE_MODE=discard`로 실행합니다. 기본값 `retry`는 부분 전사문을 잠깐 남겨 같은 조건의 재시도 때 API 비용을 줄입니다. **`discard`는 실패 후 재시도할 때 이미 처리된 음성도 다시 과금될 수 있습니다.**
- `scribeflow cache status`로 남은 용량을 확인하고 `scribeflow cache clean`으로 정리합니다. 별도의 상시 청소 서비스는 없습니다.
- 원격 Cloud Run Job은 실행용 임시 파일시스템이 종료 시 사라집니다. 원격 Worker는 전체 분할본을 보관하지 않고 각 검증된 조각을 처리합니다. 작업 결과·중복 방지 기록은 Firestore의 7일 TTL로, 인증 데이터는 자체 유효기간에 따라 자동 삭제됩니다. SDK의 자동 재요청은 사용하지 않으며, 명시적 재시도는 이미 처리한 구간을 다시 과금할 수 있습니다.

## 전사 정확도 비교 (별도 API 요금 없음)

같은 녹음을 대상으로 기존·개선 방식으로 만든 TXT와 **사람이 직접 검수한 기준 TXT**가 있으면 문자 오류율(CER)을 비교할 수 있습니다. 이 명령은 로컬 파일 세 개를 읽고 결과를 화면에 출력할 뿐, 녹음이나 TXT를 수정하거나 OpenAI API를 호출하지 않습니다.

```bash
scribeflow evaluate 기준.txt 기존.txt 개선.txt --keyword '하나손해보험' --keyword '대인접수'
```

출력되는 `baseline.cer`와 `candidate.cer`는 각각 기준 원문에 대한 정규화된 문자 편집 거리 비율로 **낮을수록 좋습니다**. 비교할 때 공백·문장부호·오디오 구간 표시·TXT 끝의 메타데이터와 해시를 제외하고 영문 대소문자를 통일합니다. `cer_reduction`은 기존 오류율에서 개선 오류율을 뺀 값입니다. `keyword_occurrences_unaligned`는 전사문 전체에 나타난 용어 수만 세므로 특정 발언을 정확하게 맞혔다는 증거는 아닙니다. 사람 검수본이 없으면 정확도를 검증했다고 주장할 수 없습니다.

## 테스트

```bash
uv run --locked --extra cloud --python 3.11 python -m unittest discover -s tests -v
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
