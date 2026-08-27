"""Video: MP4 bodies, sniffed by the `ftyp` box's own major brand. `ftyp` at
offset 4 is shared by the whole ISO-BMFF family -- HEIC, M4A, MOV, 3GP and
more all start with it too -- so the brand at offset 8 is checked against a
known-MP4 set rather than accepting every `ftyp`-prefixed body; otherwise a
HEIC photo or an M4A track could fall through base.py's every-handler-sniff
fallback and land here mislabeled as video. Duration comes from shelling out
to ffprobe against a throwaway temp file -- `handle` only ever gets bytes,
not a path. ffprobe is opportunistic (CLAUDE.md): missing from PATH, or
present but with nothing in the container for it to read
(tests/fake_api/payloads.py's tiny_video_no_duration is exactly that second
case), both land as a null duration with a reason -- not a retryable failure
either way.
"""

import os
import shutil
import subprocess
import tempfile

from .base import Handler, HandlerResult, register

_FTYP_MAGIC = b"ftyp"
_MP4_BRANDS = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash", b"mmp4"}
_FFPROBE_TIMEOUT_SECONDS = 10


def _read_duration_seconds(body: bytes) -> tuple[float | None, str | None]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None, "ffprobe not found on PATH"

    # mkstemp, not NamedTemporaryFile: ffprobe opens this path itself while
    # our own handle would still be live otherwise, and on Windows a second
    # process can't open a file the first still holds open for writing. The
    # fd is closed (the `with` below) before ffprobe ever touches the path,
    # and the file is unlinked by hand once ffprobe is done with it either way.
    fd, path = tempfile.mkstemp(suffix=".mp4")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(body)
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=_FFPROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, "ffprobe timed out"
    finally:
        os.unlink(path)

    output = proc.stdout.strip()
    if proc.returncode != 0 or not output:
        return None, "ffprobe could not determine duration"
    try:
        return float(output), None
    except ValueError:
        return None, "ffprobe could not determine duration"


@register
class VideoHandler(Handler):
    kind = "video"
    directory = "videos"
    extension = "mp4"
    content_type = "video/mp4"

    def sniff(self, body: bytes) -> bool:
        return body[4:8] == _FTYP_MAGIC and body[8:12] in _MP4_BRANDS

    def handle(self, body: bytes, url: str) -> HandlerResult:
        duration_seconds, reason = _read_duration_seconds(body)
        metadata: dict[str, object] = {"file_size": len(body), "duration_seconds": duration_seconds}
        if reason is not None:
            metadata["duration_unavailable_reason"] = reason
        return HandlerResult(metadata=metadata, links=[])
