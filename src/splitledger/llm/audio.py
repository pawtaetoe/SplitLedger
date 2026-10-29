import asyncio
import tempfile
from pathlib import Path

# what muse voice transcribe takes, mono 16 bit pcm at 16 khz with no metadata
WAV = ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-map_metadata", "-1"]


# any audio as a wav file through ffmpeg, unchanged when it is one already
async def to_wav(audio: bytes) -> bytes:
    if audio[:4] == b"RIFF":
        return audio
    with tempfile.TemporaryDirectory() as folder:
        source, target = Path(folder) / "voice", Path(folder) / "voice.wav"
        source.write_bytes(audio)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source), *WAV, str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, error = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {error.decode(errors='replace')[:200]}")
        return target.read_bytes()
