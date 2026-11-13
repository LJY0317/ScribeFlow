import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from filelock import FileLock

from scribeflow.cache import MAX_AGE_SECONDS, cache_stats, clean_cache
from scribeflow.pipeline import (
    _drive_file_id, cache_root, paths_for, plain_transcript, remote_result_url,
    resolve_source, run_rclone, scan_drive, transcribe,
)
from scribeflow.provenance import source_fingerprint, stamp_transcript


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
        with patch("scribeflow.pipeline.run_rclone", return_value=response):
            self.assertEqual(resolve_source(file_id), "gdrive:example/recording.m4a")
            self.assertEqual(
                paths_for(f"https://drive.google.com/file/d/{file_id}/view").transcript,
                "gdrive:example/recording.txt",
            )

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
    manifest = {"chunks": [{"index": 1, "start": 0, "end": 10}]}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "transcripts" / "0001.txt").write_text("테스트 전사문\n", encoding="utf-8")
    return directory / "transcript.md"


class PipelineTests(unittest.TestCase):
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
                        self.assertEqual(
                            remote_files[result_path].decode(),
                            stamp_transcript("테스트 전사문\n", hashlib.sha256(b"mock-audio").hexdigest()),
                        )
                        self.assertEqual(cache_stats(cache)["jobs"], 0)
                        self.assertFalse(list((cache / "downloads").iterdir()))
                        self.assertEqual(transcribe(voice)["status"], "skipped_existing")
                        self.assertEqual(engine.call_count, 1)

    def test_multi_chunk_output_contains_only_recognized_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "transcripts").mkdir()
            (root / "manifest.json").write_text(json.dumps({
                "chunks": [
                    {"index": 1, "start": 0, "end": 120},
                    {"index": 2, "start": 117, "end": 200},
                ]
            }), encoding="utf-8")
            (root / "transcripts" / "0001.txt").write_text(
                "질문이 있습니다.\n", encoding="utf-8"
            )
            (root / "transcripts" / "0002.txt").write_text(
                "네, 말씀하세요.\n", encoding="utf-8"
            )
            self.assertEqual(
                plain_transcript(root), "질문이 있습니다.\n\n네, 말씀하세요.\n"
            )

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
                    self.assertEqual(
                        (location / "recording.txt").read_text(),
                        stamp_transcript("테스트 전사문\n", hashlib.sha256(b"do-not-edit-source").hexdigest()),
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
