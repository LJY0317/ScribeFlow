"""Audio inspection, chunk creation, incremental transcription and export."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from .credentials import get_api_key

MODEL = "gpt-transcribe"
MAX_UPLOAD_BYTES = 25_000_000  # OpenAI file limit in bytes.
CHUNK_TARGET_BYTES = 24_000_000  # Headroom for stream-copied container overhead.
SUPPORTED_AUDIO_SUFFIXES = frozenset({
    ".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm",
})


def auto_chunk_seconds(duration: float, size_bytes: int) -> int:
    """Estimate a safe copy-only chunk length from the real file size."""
    if size_bytes <= MAX_UPLOAD_BYTES:
        return max(1, int(duration) + 1)
    return max(1, int(duration * CHUNK_TARGET_BYTES / size_bytes * 0.85))


@dataclass(frozen=True)
class Chunk:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def name(self) -> str:
        return f"{self.index:04d}"

    def as_dict(self) -> dict:
        return {"index": self.index, "start": self.start, "end": self.end}


def require_audio_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise RuntimeError("필수 프로그램이 없습니다: " + ", ".join(missing))


def inspect_audio(source: Path) -> dict:
    """Get duration and first audio codec without modifying the source."""
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("입력 경로가 파일이 아닙니다.")
    require_audio_tools()
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_format", "-show_streams",
            "-of", "json", str(source),
        ],
        capture_output=True, text=True, check=True,
    )
    result = json.loads(process.stdout)
    audio_streams = [
        stream for stream in result.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise ValueError("오디오 트랙을 찾지 못했습니다.")
    duration_text = result.get("format", {}).get("duration")
    if not duration_text:
        duration_text = audio_streams[0].get("duration")
    if not duration_text:
        raise ValueError("녹음 길이를 확인하지 못했습니다.")
    duration = float(duration_text)
    if not (0 < duration < float("inf")):
        raise ValueError("잘못된 오디오 길이입니다.")
    return {
        "duration_seconds": duration,
        "codec": audio_streams[0].get("codec_name", "unknown"),
        "audio_tracks": len(audio_streams),
        "size_bytes": source.stat().st_size,
    }


def plan_chunks(
    duration: float, chunk_seconds: int = 600, overlap_seconds: int = 3,
) -> list[Chunk]:
    if not (duration > 0):
        raise ValueError("녹음 길이는 0보다 커야 합니다.")
    if chunk_seconds < 1:
        raise ValueError("구간 길이는 최소 1초입니다.")
    if not (0 <= overlap_seconds < chunk_seconds / 4):
        raise ValueError("겹침은 구간 길이의 1/4 미만이어야 합니다.")

    chunks = []
    start = 0.0
    while start < duration:
        end = min(duration, start + chunk_seconds)
        # Avoid creating a final chunk containing almost nothing but overlap.
        if 0 < duration - end <= max(20, overlap_seconds + 5):
            end = duration
        chunks.append(Chunk(len(chunks) + 1, round(start, 3), round(end, 3)))
        if end >= duration:
            break
        start = end - overlap_seconds
    return chunks


def detect_silence_cut(source: Path, desired_end: float, lookback: float = 45.0) -> float | None:
    """Find a real silence near the upload boundary, using FFmpeg only.

    Silence boundaries are within the source timeline. Selecting a cut inside
    an observed silent region lets adjacent requests meet without overlap.
    When no quiet interval is found, the caller retains its overlap instead.
    """
    scan_start = max(0.0, desired_end - lookback)
    scan_length = desired_end - scan_start
    if scan_length < 1:
        return None
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
        "-ss", str(scan_start), "-i", str(source), "-t", str(scan_length),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-af", "silencedetect=noise=-35dB:d=0.6", "-f", "null", "-",
    ]
    process = subprocess.run(command, capture_output=True, text=True, check=True)
    # FFmpeg provides silence end and duration; use the middle of the region.
    candidates: list[float] = []
    for end_str, duration_str in re.findall(
        r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)",
        process.stderr,
    ):
        relative_center = float(end_str) - float(duration_str) / 2
        center = scan_start + relative_center
        if scan_start + 1 <= center <= desired_end - 1:
            candidates.append(center)
    return round(max(candidates), 3) if candidates else None


def plan_audio_chunks(
    source: Path, duration: float, chunk_seconds: int,
    overlap_seconds: int = 3,
) -> list[Chunk]:
    """Minimize splits, prefer silence, retain overlap for active speech."""
    if duration <= chunk_seconds:
        return plan_chunks(duration, chunk_seconds, overlap_seconds)
    if chunk_seconds < 1 or overlap_seconds < 0 or overlap_seconds >= chunk_seconds / 4:
        raise ValueError("잘못된 전사 분할 설정입니다.")
    result: list[Chunk] = []
    start = 0.0
    while start < duration:
        desired_end = min(duration, start + chunk_seconds)
        if desired_end >= duration:
            end = duration
            next_start = end
        else:
            cut = detect_silence_cut(source, desired_end) if chunk_seconds >= 60 else None
            if cut is not None and cut > start + min(30, chunk_seconds / 4):
                end = cut
                next_start = end
            else:
                end = desired_end
                next_start = end - overlap_seconds
        result.append(Chunk(len(result) + 1, round(start, 3), round(end, 3)))
        if end >= duration:
            break
        start = next_start
    return result


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as infile:
        for block in iter(lambda: infile.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_text_atomic(path: Path, content: str) -> None:
    temp = path.with_name(path.name + ".part")
    try:
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def render_chunk(
    source: Path, chunk: Chunk, destination: Path, *, is_last: bool = False,
) -> None:
    """Copy audio packets with accurate boundaries, without re-encoding.

    Input-side -ss seeks to an earlier container/keyframe boundary, especially
    in Ogg/WebM, which can duplicate seconds of audio between chunks. Output-
    side -ss discards packets up to the requested boundary without decoding.
    Omit -ss for the first chunk to retain encoder priming packets, and omit
    -t for the last to avoid losing a trailing packet in some containers.
    """
    temp = destination.with_name(destination.stem + ".part" + destination.suffix)
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-y", "-i", str(source),
    ]
    if chunk.start > 0:
        command.extend(["-ss", str(chunk.start)])
    if not is_last:
        command.extend(["-t", str(chunk.duration)])
    command.extend([
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-c:a", "copy", str(temp),
    ])
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise ValueError(f"{chunk.name}번 구간이 비어 있습니다.")
        temp.replace(destination)
    finally:
        if temp.exists():
            temp.unlink()


def prepare_stream_copy_chunks(
    source: Path, duration: float, initial_seconds: int,
    overlap_seconds: int, destination: Path,
) -> list[Chunk]:
    """Preflight every chunk's actual bytes before making any paid API calls.

    For VBR audio or high-overhead containers, shrink the planned duration and
    try again. The source packets are copied unchanged; there is no lossy
    conversion or audio re-encoding.
    """
    seconds = initial_seconds
    with tempfile.TemporaryDirectory(prefix="scribeflow-copy-") as directory:
        scratch = Path(directory)
        while True:
            for previous in scratch.iterdir():
                previous.unlink()
            # Short chunks cannot accommodate a full three-second overlap.
            effective_overlap = min(overlap_seconds, max(0, (seconds - 1) // 4))
            chunks = plan_audio_chunks(source, duration, seconds, effective_overlap)
            oversized = False
            for chunk in chunks:
                path = scratch / f"{chunk.name}{source.suffix.lower()}"
                render_chunk(source, chunk, path, is_last=chunk.index == len(chunks))
                if path.stat().st_size > CHUNK_TARGET_BYTES:
                    oversized = True
                    break
            if not oversized:
                destination.mkdir(parents=True, exist_ok=True)
                for path in scratch.iterdir():
                    path.replace(destination / path.name)
                return chunks
            if seconds == 1:
                raise ValueError(
                    "무재인코딩 1초 구간도 업로드 한도를 초과합니다. "
                    "원본을 자동으로 손실 재인코딩하지 않습니다."
                )
            seconds = max(1, int(seconds * 0.75))


def clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def transcribe_file(
    source: Path, output: Path, chunk_seconds: int | None = None,
    overlap_seconds: int = 3, language: str = "ko",
    keywords: tuple[str, ...] = (), prompt: str = "",
) -> Path:
    """Transcribe the original file when possible, otherwise stream-copy chunks."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError(
            "API 키가 없습니다. macOS에서는 'scribeflow auth set' 명령으로 "
            "키체인에 저장하거나 OPENAI_API_KEY 환경변수를 설정하세요."
        )

    source = source.expanduser().resolve(strict=True)
    output = output.expanduser().resolve()
    info = inspect_audio(source)
    if source.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError(
            f"OpenAI 전사 API가 지원하지 않는 파일 형식입니다: {source.suffix}. "
            "ScribeFlow는 손실 재인코딩을 자동으로 하지 않습니다."
        )
    if chunk_seconds is not None and chunk_seconds < 1:
        raise ValueError("구간 길이는 최소 1초입니다.")
    if overlap_seconds < 0:
        raise ValueError("겹침 길이는 0초 이상이어야 합니다.")

    direct = info["size_bytes"] <= MAX_UPLOAD_BYTES and (
        chunk_seconds is None or chunk_seconds >= info["duration_seconds"]
    )
    source_sha256 = hash_file(source)
    base_config = {
        "schema": 2,
        "source_filename": source.name,
        "source_sha256": source_sha256,
        "source_size": info["size_bytes"],
        "duration_seconds": info["duration_seconds"],
        "model": MODEL,
        "encoding": "original" if direct else "stream-copy",
        "language": language,
        "keywords": list(keywords),
        "prompt": prompt,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
    }
    manifest = output / "manifest.json"
    chunk_dir = output / "audio_chunks"
    text_dir = output / "transcripts"

    if manifest.exists():
        previous = json.loads(manifest.read_text(encoding="utf-8"))
        chunks = [Chunk(**data) for data in previous.get("chunks", [])]
        config = {**base_config, "chunks": [chunk.as_dict() for chunk in chunks]}
        if not chunks or config != previous:
            raise ValueError(
                "이 출력 폴더의 원본 파일 또는 전사 설정이 다릅니다. "
                "다른 출력 폴더를 사용하세요."
            )
    else:
        if output.exists() and any(output.iterdir()):
            raise ValueError("출력 폴더에 다른 파일이 있습니다. 빈 폴더를 지정하세요.")
        if direct:
            chunks = [Chunk(1, 0.0, round(info["duration_seconds"], 3))]
        else:
            seconds = chunk_seconds if chunk_seconds is not None else auto_chunk_seconds(
                info["duration_seconds"], info["size_bytes"]
            )
            chunks = prepare_stream_copy_chunks(
                source, info["duration_seconds"], seconds,
                overlap_seconds, chunk_dir,
            )
        config = {**base_config, "chunks": [chunk.as_dict() for chunk in chunks]}
        output.mkdir(parents=True, exist_ok=True)
        write_text_atomic(manifest, json.dumps(config, ensure_ascii=False, indent=2))

    chunk_dir.mkdir(exist_ok=True)
    text_dir.mkdir(exist_ok=True)

    client = None
    for chunk in chunks:
        text_path = text_dir / (chunk.name + ".txt")
        if text_path.exists():
            print(f"[{chunk.index}/{len(chunks)}] 기존 전사문 재사용", file=sys.stderr)
            continue
        audio_path = (
            source if direct else chunk_dir / f"{chunk.name}{source.suffix.lower()}"
        )
        if not direct and not audio_path.exists():
            render_chunk(source, chunk, audio_path, is_last=chunk.index == len(chunks))
        if not (0 < audio_path.stat().st_size <= MAX_UPLOAD_BYTES):
            raise ValueError(f"{chunk.index}번 오디오 구간 크기가 유효하지 않습니다.")
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
        print(
            f"[{chunk.index}/{len(chunks)}] "
            f"{clock(chunk.start)}~{clock(chunk.end)} 전사 요청",
            file=sys.stderr, flush=True,
        )
        payload = {"model": MODEL, "languages": [language]}
        if keywords:
            payload["keywords"] = list(keywords)
        if prompt:
            payload["prompt"] = prompt
        with audio_path.open("rb") as audio:
            response = client.audio.transcriptions.create(file=audio, **payload)
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise RuntimeError("API에서 텍스트 결과를 받지 못했습니다.")
        write_text_atomic(text_path, text.strip() + "\n")

    return output
