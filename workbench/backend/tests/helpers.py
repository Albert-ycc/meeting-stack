import hashlib
from pathlib import Path

from meeting_workbench.db import Database, utc_now


def seed_editable_meeting(db: Database, root: Path, meeting_id: str = "vm-20260102-101500"):
    meeting_dir = root / meeting_id
    whisper_dir = meeting_dir / "whisper-ref"
    whisper_dir.mkdir(parents=True)
    audio = meeting_dir / f"{meeting_id}.m4a"
    audio.write_bytes(b"immutable-audio-payload")
    sources = {
        "audio": audio,
        "funasr_json": meeting_dir / f"{meeting_id}.funasr.json",
        "funasr_log": meeting_dir / f"{meeting_id}.funasr.log",
        "speaker_map": meeting_dir / "spk.txt",
        "whisper_json": whisper_dir / f"{meeting_id}.json",
        "whisper_txt": whisper_dir / f"{meeting_id}.txt",
        "whisper_srt": whisper_dir / f"{meeting_id}.srt",
        "whisper_tsv": whisper_dir / f"{meeting_id}.tsv",
        "whisper_vtt": whisper_dir / f"{meeting_id}.vtt",
        "whisper_log": whisper_dir / "whisper.log",
    }
    for kind, path in sources.items():
        if kind != "audio":
            path.write_text("{}" if path.suffix == ".json" else "ok", encoding="utf-8")
    audio_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    db.execute(
        """INSERT INTO meetings
           (id, title, status, canonical_dir, source_priority, original_audio_sha256, created_at, updated_at)
           VALUES (?, '需求复盘会', 'completed_unreviewed', ?, 100, ?, ?, ?)""",
        (meeting_id, str(meeting_dir), audio_hash, utc_now(), utc_now()),
    )
    for kind, path in sources.items():
        stat = path.stat()
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
               VALUES (?, ?, 'source', 'archive', ?, ?, ?, ?)""",
            (meeting_id, kind, str(path), stat.st_size, stat.st_mtime_ns, utc_now()),
        )
    version = db.create_transcript_version(meeting_id, "funasr", published=True)
    db.replace_segments(
        version,
        meeting_id,
        [
            {
                "id": "seg-a",
                "ordinal": 0,
                "start_ms": 1000,
                "end_ms": 3000,
                "speaker_label": "SPEAKER_00",
                "text": "第一段内容",
            },
            {
                "id": "seg-b",
                "ordinal": 1,
                "start_ms": 3200,
                "end_ms": 5000,
                "speaker_label": "SPEAKER_01",
                "text": "第二段内容",
            },
        ],
    )
    return meeting_dir, audio, version
