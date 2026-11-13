# ScribeFlow

긴 한국어 녹음을 OpenAI `gpt-transcribe` 모델 하나로 전사하는 가벼운 CLI입니다. 로컬 오디오와 Google Drive를 지원합니다. **최종 산출물은 원본과 같은 폴더, 같은 기본 이름의 `.txt` 파일 하나**입니다. 요약·의견·제목·화자를 임의로 만들지 않습니다. 마지막에는 출처 검증을 위한 원본 SHA-256 한 줄만 붙습니다.

## 설치

- Python 3.10+, [uv](https://docs.astral.sh/uv/), FFmpeg/FFprobe
- OpenAI API 키와 사용 가능한 API 크레딧
- Google Drive를 사용할 경우 [rclone](https://rclone.org/drive/)의 `gdrive:` 연결 (로컬 파일 처리에는 불필요)

```bash
cd /path/to/ScribeFlow
uv sync
uv run scribeflow auth set       # macOS: API 키를 키체인에 비공개 저장
uv run scribeflow auth status
```

키는 채팅, 저장소 또는 명령 인수에 붙여넣지 마세요. macOS 이외에서는 `OPENAI_API_KEY` 환경변수를 사용합니다. `auth status`는 키 저장 여부만 검사합니다.

## 사용법

```bash
# 로컬 녹음 → 같은 폴더에 '녹음.txt'
uv run scribeflow inspect '/path/to/녹음.m4a'
uv run scribeflow transcribe '/path/to/녹음.m4a' --json

# 전문용어가 실제로 등장하는 녹음에만 인식 힌트를 선택적으로 제공
uv run scribeflow transcribe '/path/to/다른녹음.m4a' --keyword 'GitLab' --keyword 'EC2'
```

### Google Drive

1. `rclone config`로 Google Drive 원격 저장소의 이름을 `gdrive`로 설정합니다.
2. 음성 파일의 **Google Drive URL, 파일 ID 또는 `gdrive:폴더/녹음.m4a` 경로**를 전달합니다.
3. ScribeFlow가 Drive에 있는 실제 파일을 식별하고, 로컬 임시 캐시에 다운로드한 뒤 전사합니다. 완료 후 `녹음.txt`를 원본과 같은 Drive 폴더에 업로드합니다.

```bash
# 큰 Drive에서 탐색 범위를 제한하려면 해당 폴더의 ID를 선택적으로 설정
export SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID='구글드라이브_폴더_ID'

# Drive 폴더의 아직 전사하지 않은 음성 파일 검색 (전사 API 호출 없음)
uv run scribeflow scan gdrive: --recursive --json

# 파일 URL 입력 (일반 Google Drive 파일 보기 링크)
uv run scribeflow paths 'https://drive.google.com/file/d/파일_ID/view'
uv run scribeflow transcribe 'https://drive.google.com/file/d/파일_ID/view' --json

# 파일 ID로도 가능
uv run scribeflow transcribe '파일_ID' --json
```

`SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID`는 선택 사항입니다. 설정하면 해당 폴더와 하위 파일에서만 검색하고, 설정하지 않으면 연결된 Drive의 기본 루트를 사용합니다. **파일 URL/ID를 처음 해석할 때 해당 범위의 파일 메타데이터를 조회**합니다. 넓은 Drive에서는 상위 폴더 ID를 지정하면 검색이 빨라집니다.

완료 결과 JSON에는 `status`, `source`, `transcript`, `model`, `url`(Drive인 경우)이 포함됩니다. 같은 폴더에 동명 TXT가 있으면 마지막 줄의 SHA-256을 원본 음성과 비교합니다. 일치하면 `skipped_existing`, 해시가 없으면 `unverified_existing`, 불일치하면 `source_mismatch`입니다. **어떤 경우에도 기존 TXT를 자동 덮어쓰거나 재전사하지 않습니다.** 미확인·불일치 결과는 종료 코드 1로 반환합니다. 재전사하려면 `--output`으로 같은 폴더의 다른 `.txt` 파일명을 지정하세요.

### 에이전트에서의 사용

에이전트는 `scribeflow scan ... --json`으로 미전사 파일을 찾고, 선택한 항목의 `source` 또는 `drive_file_id`를 `scribeflow transcribe ... --json`에 전달한 뒤 `url`을 사용자에게 안내하면 됩니다. 실행 주체가 실제 맥북 셸에 접근해야 합니다. ChatGPT가 이 프로젝트를 언제나 자동 호출하는 별도 플러그인이나 상시 서비스는 설치되어 있지 않습니다.

## 원문 보존과 오디오 처리

- **본문은 음성에서 인식된 발언만 출력**합니다. 문장부호·문단 구분·공백만 정리하며 요약, 결론, AI가 만든 화자 정보, 가상 발언을 넣지 않습니다. 맨 끝에는 `<!-- ScribeFlow SHA256: 64자리해시 -->` 형식으로 원본 파일의 SHA-256만 한 줄 기록합니다. 본문에 포함된 이 주석은 음성 발언이 아닙니다.
- SHA-256은 파일명·Drive 파일 ID가 달라져도 **동일한 바이트의 음성 파일인지** 비교할 수 있습니다. Drive가 해시를 제공하면 원본 전체를 다시 내려받지 않고 검증합니다. 다만 파일 재인코딩 시 값이 달라지며, 해시 일치는 전사문 발언 내용의 정확성을 증명하지 않습니다.
- MP3 128kbps로 변환하여 API의 25MB 업로드 제한 아래로 맞춥니다. 가능한 한 적은 구간으로 분할합니다. 분할 지점에서 침묵을 발견하면 그곳을 사용해 중복을 피하고, 없으면 3초 겹침을 유지합니다. 이 경우 구간 경계의 발언이 중복될 수 있으며, 임의로 제거하지 않습니다.
- 최종 사용자 폴더에는 원본 오디오와 같은 이름의 `.txt`만 추가됩니다. Drive 원본도 변경하지 않습니다.
- 전사 시 오디오 내용은 OpenAI API로 전송됩니다. 개인정보 및 녹음 활용 권한을 확인하세요.

## 임시 캐시와 재시도

캐시는 macOS에서 `~/Library/Caches/ScribeFlow`에 저장됩니다(다른 OS는 표준 사용자 캐시 경로). `SCRIBEFLOW_CACHE_DIR`로 변경할 수 있습니다. MP3 분할본, 구간별 전사문, manifest는 모두 여기에서만 사용합니다.

- **성공 후 즉시 제거**, 실패한 작업만 이어서 실행할 수 있도록 보관합니다.
- 실패 캐시는 7일 또는 전체 1GiB 한도 기준으로 오래된 작업부터 정리합니다. 활성 작업은 잠금으로 보호합니다.
- `scribeflow cache status`로 확인하고 `scribeflow cache clean`으로 정리합니다. 정리는 전사 시작 때도 수행합니다. 상시 백그라운드 서비스는 없습니다.
- 실패 캐시를 삭제하거나 API에 이미 도달한 요청이 중단되면 재시도 시 비용이 추가 발생할 수 있습니다.

## 테스트

```bash
uv run python -m unittest discover -s tests -v
```

현재 별도의 웹 UI, 화자 자동 식별 또는 AI 요약 모델은 포함하지 않습니다.
