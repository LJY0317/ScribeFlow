"""Command-line interface for ScribeFlow."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .cache import cache_stats, clean_cache
from .credentials import get_api_key, remove_saved_key, save_interactive
from .engine import inspect_audio
from .pipeline import cache_root, paths_for, scan_drive, transcribe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scribeflow",
        description="한국어 긴 녹음을 분할하고 OpenAI GPT-Transcribe로 전사합니다.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="macOS 키체인에 API 키 저장·확인·삭제")
    auth.add_argument("action", choices=["set", "status", "remove"])

    inspect = sub.add_parser("inspect", help="녹음 형식과 길이 확인 (API 요금 없음)")
    inspect.add_argument("audio", type=Path)

    paths = sub.add_parser("paths", help="원본과 최종 전사문 위치 확인 (API 호출 없음)")
    paths.add_argument("audio")

    scan = sub.add_parser("scan", help="Google Drive 폴더의 미전사 음성 파일 검색 (API 호출 없음)")
    scan.add_argument("folder", help="gdrive: 또는 gdrive:하위폴더")
    scan.add_argument("--recursive", action="store_true")
    scan.add_argument("--limit", type=int, default=50)
    scan.add_argument("--json", action="store_true", help="에이전트용 JSON 출력")

    cache = sub.add_parser("cache", help="실패한 작업의 임시 캐시 확인·정리")
    cache.add_argument("action", choices=["status", "clean"])

    command = sub.add_parser("transcribe", help="구간별 전사 후 하나의 텍스트 저장")
    command.add_argument("audio", help="로컬 경로 또는 gdrive:폴더/녹음.m4a")
    command.add_argument("--output", help="동일한 폴더에 저장할 .txt 파일명")
    command.add_argument(
        "--chunk-seconds", type=int, default=None,
        help="구간 길이(초). 미지정 시 음질을 유지하며 25MB 한도 내 최대한 길게",
    )
    command.add_argument("--overlap-seconds", type=int, default=3)
    command.add_argument("--language", default="ko")
    command.add_argument(
        "--keyword", action="append", default=[],
        help="고유명사 힌트 (여러 번 지정 가능)",
    )
    command.add_argument("--prompt", default="", help="녹음에 관한 간단한 맥락")
    command.add_argument(
        "--json", action="store_true",
        help="에이전트용: 전사 완료 후 결과 경로를 JSON으로 출력",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "auth":
            if args.action == "set":
                save_interactive()
                print("macOS 키체인에 API 키를 저장했습니다. API 크레딧 연결은 별도입니다.")
            elif args.action == "status":
                print(json.dumps({
                    "configured": bool(get_api_key()),
                    "note": "키 저장 상태만 확인하며 API 크레딧이나 키 유효성은 확인하지 않습니다.",
                }, ensure_ascii=False))
            else:
                remove_saved_key()
                print("ScribeFlow에 저장된 API 키를 삭제했습니다.")
        elif args.command == "inspect":
            print(json.dumps(
                inspect_audio(args.audio), ensure_ascii=False, indent=2,
            ))
        elif args.command == "paths":
            output = paths_for(args.audio)
            print(json.dumps(
                {"source": output.source, "transcript": output.transcript},
                ensure_ascii=False,
            ))
        elif args.command == "scan":
            if not 1 <= args.limit <= 1000:
                raise ValueError("--limit는 1~1000 사이여야 합니다.")
            result = scan_drive(args.folder, recursive=args.recursive, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
        elif args.command == "cache":
            result = clean_cache(cache_root()) if args.action == "clean" else cache_stats(cache_root())
            print(json.dumps(result, ensure_ascii=False))
        else:
            result = transcribe(
                args.audio, args.output, chunk_seconds=args.chunk_seconds,
                overlap_seconds=args.overlap_seconds,
                language=args.language,
                keywords=tuple(args.keyword), prompt=args.prompt,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"\n{result['status']}: {result['transcript']}")
            if not result.get("ok", False):
                return 1
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
