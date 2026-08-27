"""handlers/video.py: MP4/ISO-BMFF sniffed by its ftyp box; duration read by
shelling out to ffprobe. ffprobe's real presence and behaviour aren't
something a test should depend on, so both are faked at the subprocess
boundary instead of relying on whatever's actually on this machine's PATH.
"""

import subprocess
from unittest.mock import patch

from fake_api.payloads import tiny_pdf, tiny_video_no_duration

from crawler.handlers.video import VideoHandler

HANDLER = VideoHandler()


class TestSniff:
    def test_mp4_ftyp_box_matches(self):
        assert HANDLER.sniff(tiny_video_no_duration())

    def test_pdf_does_not_match(self):
        assert not HANDLER.sniff(tiny_pdf())

    def test_non_mp4_ftyp_brand_does_not_match(self):
        # HEIC, M4A, MOV and others all share the ftyp box at offset 4 —
        # only a recognized MP4 brand at offset 8 should route here.
        heic = (24).to_bytes(4, "big") + b"ftypheic" + b"\x00" * 8
        assert not HANDLER.sniff(heic)


class TestHandle:
    def test_missing_ffprobe_yields_null_duration_with_reason(self):
        body = tiny_video_no_duration()
        with patch("crawler.handlers.video.shutil.which", return_value=None):
            result = HANDLER.handle(body, "http://fixture.local/clip.mp4")

        assert result.metadata["file_size"] == len(body)
        assert result.metadata["duration_seconds"] is None
        assert result.metadata["duration_unavailable_reason"] == "ffprobe not found on PATH"
        assert result.links == []

    def test_ffprobe_present_with_no_duration_yields_null_with_reason(self):
        # tiny_video_no_duration is a bare ftyp box with no moov box for
        # ffprobe to read a duration from -- see its own docstring.
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with (
            patch("crawler.handlers.video.shutil.which", return_value="/usr/bin/ffprobe"),
            patch("crawler.handlers.video.subprocess.run", return_value=completed),
        ):
            result = HANDLER.handle(tiny_video_no_duration(), "http://fixture.local/clip.mp4")

        assert result.metadata["duration_seconds"] is None
        assert result.metadata["duration_unavailable_reason"] == "ffprobe could not determine duration"

    def test_ffprobe_reports_a_duration(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="12.5\n", stderr="")
        with (
            patch("crawler.handlers.video.shutil.which", return_value="/usr/bin/ffprobe"),
            patch("crawler.handlers.video.subprocess.run", return_value=completed),
        ):
            result = HANDLER.handle(tiny_video_no_duration(), "http://fixture.local/clip.mp4")

        assert result.metadata["duration_seconds"] == 12.5
        assert "duration_unavailable_reason" not in result.metadata
