import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from filelock import FileLock

from scribeflow.cache import MAX_AGE_SECONDS, cache_stats, clean_cache
from scribeflow.pipeline import (
    _drive_file_id, assemble_transcript, cache_root, paths_for, remote_result_url,
    resolve_source, run_rclone, scan_drive, transcribe,
)
from scribeflow.provenance import (
    TranscriptMetadata,
    source_fingerprint,
    stamp_transcript,
    strip_transcript_footer,
    transcript_model,
)


class FilePathTests(unittest.TestCase):
    def test_default_cache_stays_in_source_checkout(self):
        with patch.dict("os.environ", {"SCRIBEFLOW_CACHE_DIR": ""}):
            checkout = Path(__file__).resolve().parents[1]
            self.assertEqual(cache_root(), checkout / ".scribeflow" / "cache")

    def test_explicit_cache_location_overrides_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"SCRIBEFLOW_CACHE_DIR": directory}):
                self.assertEqual(cache_root(), Path(directory).resolve())

    def test_scan_returns_only_audio_missing_transcript(self):
        audio_hash = hashlib.sha256(b"test-audio").hexdigest()
        entries = [
            {"Path": "example/recording.m4a", "MimeType": "audio/x-m4a", "Size": 1024, "ID": "a", "Hashes": {"sha256": audio_hash}},
            {"Path": "example/recording.txt", "MimeType": "text/plain", "ID": "b"},
            {"Path": "example/lecture.m4a", "MimeType": "audio/x-m4a", "Size": 2048, "ID": "c"},
            {"Path": "example/notes.pdf", "MimeType": "application/pdf", "ID": "d"},
        ]
        def fake_rclone(*args):
            content = json.dumps(entries) if args[0] == "lsjson" else stamp_transcript("녹취록\n", audio_hash)
            return type("R", (), {"stdout": content})()
        with patch("scribeflow.pipeline.run_rclone", side_effect=fake_rclone):
            result = scan_drive("gdrive:", recursive=True)
        self.assertEqual(result["total_audio"], 2)
        self.assertEqual(result["already_transcribed"], 1)
        self.assertEqual(result["untranscribed_count"], 1)
        self.assertEqual(result["untranscribed"][0]["source"], "gdrive:example/lecture.m4a")

    def test_model_inventory_uses_txt_footers_and_reports_legacy_as_unknown(self):
        sha = hashlib.sha256(b"audio").hexdigest()
        metadata = TranscriptMetadata("new.m4a", 5, 10, "OpenAI", "model-old", datetime.now(timezone.utc))
        entries = [
            {"Path": name + ".m4a", "ID": name + "-audio", "Hashes": {"sha256": sha}}
            for name in ("legacy", "new")
        ] + [{"Path": name + ".txt", "ID": name + "-txt"} for name in ("legacy", "new")]
        with patch("scribeflow.pipeline.run_rclone") as rclone:
            def reply(*args):
                if args[0] == "lsjson":
                    return type("R", (), {"stdout": json.dumps(entries)})()
                details = metadata if args[1] == "gdrive:new.txt" else None
                return type("R", (), {"stdout": stamp_transcript("본문", sha, details)})()

            rclone.side_effect = reply
            result = scan_drive("gdrive:", include_models=True)
        legacy, current = result["transcribed"]
        self.assertIsNone(legacy["model"])
        self.assertEqual(current["drive_file_id"], "new-audio")
        self.assertEqual(current["model"]["model_id"], "model-old")
        self.assertIsNone(current["model"]["snapshot"])
        self.assertEqual(result["model_counts"], [
            {"model_id": None, "count": 1}, {"model_id": "model-old", "count": 1},
        ])

    def test_local_same_folder_and_stem(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "녹음.m4a"
            result = paths_for(str(source))
            self.assertEqual(result.transcript, str(source.resolve().with_suffix(".txt")))
            with self.assertRaises(ValueError):
                paths_for(str(source), str(Path(directory).parent / "other.txt"))

    def test_remote_relative_paths(self):
        self.assertEqual(
            paths_for("gdrive:모임/녹음.m4a").transcript,
            "gdrive:모임/녹음.txt",
        )
        for path in ("gdrive:../other.m4a", "gdrive:folder/../other.m4a"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                paths_for(path)
        with self.assertRaises(ValueError):
            paths_for("gdrive:모임/녹음.m4a", "gdrive:다른폴더/다른이름.txt")

    def test_optional_drive_scope(self):
        with patch.dict("os.environ", {"SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID": ""}):
            with patch("scribeflow.pipeline.shutil.which", return_value="rclone"):
                with patch("scribeflow.pipeline.subprocess.run") as runner:
                    run_rclone("lsf", "gdrive:")
                    self.assertNotIn("--drive-root-folder-id", runner.call_args.args[0])

    def test_url_id_resolution(self):
        file_id = "synthetic_drive_audio_file_0000001"
        self.assertEqual(_drive_file_id(f"https://drive.google.com/file/d/{file_id}/view"), file_id)
        self.assertEqual(_drive_file_id(f"https://drive.google.com/open?id={file_id}"), file_id)
        self.assertEqual(_drive_file_id(file_id), file_id)
        self.assertIsNone(_drive_file_id("/private/var/audio.m4a"))
        response = type("R", (), {"stdout": json.dumps([
            {"ID": file_id, "Path": "example/recording.m4a", "MimeType": "audio/x-m4a"}
        ])})()
        with patch.dict(os.environ, {"SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID": "example_folder"}):
            with patch("scribeflow.pipeline.run_rclone", return_value=response):
                self.assertEqual(resolve_source(file_id), "gdrive:example/recording.m4a")
                self.assertEqual(
                    paths_for(f"https://drive.google.com/file/d/{file_id}/view").transcript,
                    "gdrive:example/recording.txt",
                )

    def test_shared_with_me_audio_stays_in_shared_scope(self):
        file_id = "shared_drive_audio_file_00001"
        response = type("R", (), {"stdout": json.dumps([
            {"ID": file_id, "Path": "Shared folder/recording.m4a", "MimeType": "audio/x-m4a"}
        ])})()
        with patch.dict(os.environ, {
            "SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID": "",
            "RCLONE_DRIVE_SHARED_WITH_ME": "",
        }):
            os.environ.pop("RCLONE_DRIVE_SHARED_WITH_ME")
            with patch("scribeflow.pipeline.run_rclone", return_value=response):
                self.assertEqual(resolve_source(file_id), "gdrive:Shared folder/recording.m4a")
            self.assertEqual(os.environ["RCLONE_DRIVE_SHARED_WITH_ME"], "true")

    def test_uploaded_url_is_discovered_from_actual_id(self):
        response = type("R", (), {"stdout": json.dumps([
            {"Name": "recording.txt", "ID": "final_id_1234567890123456"}
        ])})()
        with patch("scribeflow.pipeline.run_rclone", return_value=response):
            self.assertEqual(
                remote_result_url("gdrive:example/recording.txt"),
                "https://drive.google.com/file/d/final_id_1234567890123456/view",
            )


def mocked_engine(source, directory, **kwargs):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transcripts").mkdir(exist_ok=True)
    manifest = {
        "source_filename": source.name,
        "source_size": source.stat().st_size,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "duration_seconds": 10,
        "model": kwargs.get("model", "gpt-transcribe"),
        "chunks": [{"index": 1, "start": 0, "end": 10}],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "transcripts" / "0001.txt").write_text("테스트 전사문\n", encoding="utf-8")
    return directory


class PipelineTests(unittest.TestCase):
    def test_explicit_drive_replacement_preserves_id_and_tracks_configured_model(self):
        audio = b"synthetic audio"
        sha = hashlib.sha256(audio).hexdigest()
        voice = "gdrive:folder/recording.m4a"
        target = "gdrive:folder/recording.txt"
        audio_id = "audio_test_123456789012"
        target_id = "transcript_test_123456789012"
        existing = stamp_transcript("기존 전사문", sha)

        def fake_rclone(command, *args):
            output = ""
            if command == "lsjson":
                self.assertEqual(args[0], "gdrive:folder")
                output = json.dumps([
                    {"Name": "recording.m4a", "ID": audio_id, "Hashes": {"sha256": sha}},
                    {"Name": "recording.txt", "ID": target_id,
                     "Hashes": {"sha256": hashlib.sha256(existing.encode()).hexdigest()}},
                ])
            elif command == "cat":
                self.assertEqual(args[0], target)
                output = existing
            elif command == "copyto":
                self.assertEqual(args[0], voice)
                Path(args[1]).write_bytes(audio)
            else:
                self.fail(f"Unexpected rclone command: {command}")
            return type("R", (), {"stdout": output})()

        def update(file_id, token, data):
            self.assertEqual(file_id, target_id)
            self.assertEqual(token, "test-token")
            self.assertEqual(transcript_model(data.decode())["model_id"], "next-model-2030-01-01")
            self.assertEqual(source_fingerprint(data.decode()), sha)
            return {"id": file_id, "sha256Checksum": hashlib.sha256(data).hexdigest()}

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"SCRIBEFLOW_TRANSCRIPTION_MODEL": "next-model-2030-01-01"}),
            patch("scribeflow.pipeline.cache_root", return_value=Path(directory)),
            patch("scribeflow.pipeline.resolve_source", return_value=voice),
            patch("scribeflow.pipeline.run_rclone", side_effect=fake_rclone),
            patch("scribeflow.pipeline._replacement_token", return_value="test-token"),
            patch("scribeflow.pipeline.transcribe_file", side_effect=mocked_engine) as engine,
            patch("scribeflow.pipeline.update_transcript", side_effect=update) as upload,
        ):
            outcome = transcribe(audio_id, replace_existing=True)
            self.assertEqual(engine.call_args.kwargs["model"], "next-model-2030-01-01")
            self.assertEqual(outcome["status"], "replaced")
            self.assertEqual(outcome["url"], f"https://drive.google.com/file/d/{target_id}/view")
            upload.assert_called_once()

            existing = stamp_transcript("다른 원본", "0" * 64)
            self.assertEqual(transcribe(audio_id, replace_existing=True)["status"], "source_mismatch")
            self.assertEqual(engine.call_count, 1)
            upload.assert_called_once()

            existing = stamp_transcript("기존 전사문", sha)

            def edit_during_transcription(source, directory, **kwargs):
                nonlocal existing
                existing = stamp_transcript("사용자가 편집한 전사문", sha)
                return mocked_engine(source, directory, **kwargs)

            engine.side_effect = edit_during_transcription
            with self.assertRaisesRegex(ValueError, "변경되었습니다"):
                transcribe(audio_id, replace_existing=True)
            self.assertIn("사용자가 편집한 전사문", existing)
            upload.assert_called_once()

    def test_concurrent_different_settings_only_call_model_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.m4a"
            source.write_bytes(b"original")
            cache = root / "cache"
            calls = []
            call_guard = threading.Lock()

            def engine(source_path, job_dir, **kwargs):
                with call_guard:
                    calls.append(tuple(kwargs.get("keywords", ())))
                time.sleep(0.06)
                return mocked_engine(source_path, job_dir, **kwargs)

            with patch("scribeflow.pipeline.cache_root", return_value=cache):
                with patch("scribeflow.pipeline.transcribe_file", side_effect=engine):
                    def run(keyword):
                        return transcribe(str(source), keywords=(keyword,))

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(run, ["a", "b"]))
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                sorted(x["status"] for x in results),
                ["created", "skipped_existing"],
            )
            self.assertEqual(cache_stats(cache)["jobs"], 0)

    def test_remote_upload_and_cleanup_without_actual_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            voice = "gdrive:example/recording.m4a"
            result_path = "gdrive:example/recording.txt"
            remote_files = {voice: b"mock-audio"}

            def fake_rclone(*args):
                command = args[0]
                if command == "lsf":
                    names = [
                        p.rsplit("/", 1)[-1] for p in remote_files
                        if p.startswith("gdrive:example/")
                    ]
                    output = "\n".join(names) + "\n"
                elif command == "copyto":
                    source, target = args[1:3]
                    if source.startswith("gdrive:"):
                        Path(target).write_bytes(remote_files[source])
                    else:
                        remote_files[target] = Path(source).read_bytes()
                    output = ""
                elif command == "cat":
                    output = remote_files[args[1]].decode("utf-8")
                elif command == "lsjson":
                    if "--hash" in args:
                        output = json.dumps([{
                            "Name": "recording.m4a",
                            "Hashes": {"sha256": hashlib.sha256(b"mock-audio").hexdigest()},
                        }])
                    else:
                        output = json.dumps([
                            {"Name": "recording.txt", "ID": "test_drive_id_1234567890"}
                        ])
                else:
                    raise AssertionError(args)
                return type("Result", (), {"stdout": output})()

            with patch("scribeflow.pipeline.cache_root", return_value=cache):
                with patch("scribeflow.pipeline.run_rclone", side_effect=fake_rclone):
                    with patch("scribeflow.pipeline.transcribe_file", side_effect=mocked_engine) as engine:
                        result = transcribe(voice)
                        self.assertEqual(result["transcript"], result_path)
                        self.assertEqual(
                            result["url"],
                            "https://drive.google.com/file/d/test_drive_id_1234567890/view",
                        )
                        self.assertEqual(engine.call_count, 1)
                        self.assertEqual(remote_files[voice], b"mock-audio")
                        transcript = remote_files[result_path].decode()
                        self.assertEqual(
                            strip_transcript_footer(transcript).strip(),
                            "[00:00:00–00:00:10]\n테스트 전사문",
                        )
                        self.assertEqual(
                            source_fingerprint(transcript),
                            hashlib.sha256(b"mock-audio").hexdigest(),
                        )
                        self.assertIn(
                            'Source: "recording.m4a" | Duration: 0:00:10 | Size: 10 bytes',
                            transcript,
                        )
                        self.assertIn("Detected languages: 확인 불가", transcript)
                        self.assertEqual(set(remote_files), {voice, result_path})
                        self.assertEqual(cache_stats(cache)["jobs"], 0)
                        self.assertFalse(list((cache / "downloads").iterdir()))
                        self.assertEqual(transcribe(voice)["status"], "skipped_existing")
                        self.assertEqual(engine.call_count, 1)

    def test_multi_chunk_output_labels_original_audio_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "transcripts").mkdir()
            chunks = [
                {"index": 1, "start": 0, "end": 120},
                {"index": 2, "start": 117.7, "end": 200.2},
            ]
            (root / "transcripts" / "0001.txt").write_text(
                "질문이 있습니다.\n", encoding="utf-8"
            )
            (root / "transcripts" / "0002.txt").write_text(
                "네, 말씀하세요.\n", encoding="utf-8"
            )
            text, languages = assemble_transcript(root, chunks)
            self.assertEqual(
                text,
                "[00:00:00–00:02:00]\n질문이 있습니다.\n\n"
                "[00:01:57–00:03:21]\n네, 말씀하세요.\n",
            )
            self.assertEqual(languages, ())

    def test_one_final_text_and_no_intermediates_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            audio = location / "recording.m4a"
            audio.write_bytes(b"do-not-edit-source")
            cache = location / "private-cache"
            with patch("scribeflow.pipeline.cache_root", return_value=cache):
                with patch("scribeflow.pipeline.transcribe_file", side_effect=mocked_engine) as engine:
                    result = transcribe(str(audio))
                    self.assertEqual(result["status"], "created")
                    self.assertEqual(result["transcript"], str((location / "recording.txt").resolve()))
                    transcript = (location / "recording.txt").read_text(encoding="utf-8")
                    self.assertEqual(
                        strip_transcript_footer(transcript).strip(),
                        "[00:00:00–00:00:10]\n테스트 전사문",
                    )
                    self.assertEqual(
                        source_fingerprint(transcript),
                        hashlib.sha256(b"do-not-edit-source").hexdigest(),
                    )
                    self.assertEqual(audio.read_bytes(), b"do-not-edit-source")
                    self.assertEqual(cache_stats(cache)["jobs"], 0)
                    self.assertEqual(transcribe(str(audio))["status"], "skipped_existing")
                    self.assertEqual(engine.call_count, 1)

    def test_failed_work_is_reusable_until_success(self):
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            source = location / "recording.m4a"
            source.write_bytes(b"original")
            cache = location / "private-cache"

            def fail_after_partial(source, output, **kwargs):
                mocked_engine(source, output, **kwargs)
                raise RuntimeError("simulated API interruption")

            with patch("scribeflow.pipeline.cache_root", return_value=cache):
                with patch("scribeflow.pipeline.transcribe_file", side_effect=fail_after_partial):
                    with self.assertRaises(RuntimeError):
                        transcribe(str(source))
                self.assertEqual(cache_stats(cache)["jobs"], 1)
                self.assertFalse((location / "recording.txt").exists())
                with patch("scribeflow.pipeline.transcribe_file", side_effect=mocked_engine):
                    transcribe(str(source))
                self.assertEqual(cache_stats(cache)["jobs"], 0)
                self.assertTrue((location / "recording.txt").exists())

    def test_clean_only_expired_owned_jobs_not_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "a" * 64
            job = root / "jobs" / key
            job.mkdir(parents=True)
            (job / "manifest.json").write_text('{"schema":1}', encoding="utf-8")
            old_time = time.time() - MAX_AGE_SECONDS - 1000
            os.utime(job, (old_time, old_time))
            lock_path = root / "locks" / (key + ".lock")
            lock_path.parent.mkdir(parents=True)
            with FileLock(str(lock_path)):
                self.assertEqual(clean_cache(root)["removed_jobs"], 0)
            self.assertEqual(clean_cache(root)["removed_jobs"], 1)
            self.assertFalse(job.exists())


if __name__ == "__main__":
    unittest.main()
