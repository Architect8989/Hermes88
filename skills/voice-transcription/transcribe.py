#!/usr/bin/env python3
"""
Voice Transcription Skill for Hermes88.
Transcribes audio files (voice messages, recordings) using OpenAI Whisper API
via DO Inference or local Whisper model fallback.

Supports: OGG/Opus, MP3, WAV, M4A, FLAC, WebM
Converts via ffmpeg when needed.

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.error

# Try to import optional dependencies
try:
    import wave
    WAVE_AVAILABLE = True
except ImportError:
    WAVE_AVAILABLE = False


class TranscriptionError(Exception):
    """Raised when transcription fails."""
    pass


class AudioConverter:
    """Converts audio files to formats suitable for Whisper API."""

    SUPPORTED_FORMATS = {".ogg", ".mp3", ".wav", ".m4a", ".flac", ".webm",
                         ".opus", ".oga", ".wma", ".aac"}
    TARGET_FORMAT = "wav"
    MAX_DURATION_SECONDS = 600  # 10 minutes
    MAX_FILE_SIZE_MB = 25

    def __init__(self):
        self.ffmpeg_available = self._check_ffmpeg()

    def _check_ffmpeg(self) -> bool:
        """Check if ffmpeg is available."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def convert_to_wav(self, input_path: str, output_path: Optional[str] = None,
                       sample_rate: int = 16000) -> str:
        """
        Convert audio file to WAV format suitable for Whisper.

        Args:
            input_path: Path to input audio file.
            output_path: Optional output path (creates temp file if None).
            sample_rate: Target sample rate (default: 16kHz for Whisper).

        Returns:
            Path to the converted WAV file.
        """
        input_file = Path(input_path)
        if not input_file.exists():
            raise TranscriptionError(f"Audio file not found: {input_path}")

        # Check file size
        file_size_mb = input_file.stat().st_size / (1024 * 1024)
        if file_size_mb > self.MAX_FILE_SIZE_MB:
            raise TranscriptionError(
                f"File too large ({file_size_mb:.1f}MB, max {self.MAX_FILE_SIZE_MB}MB)"
            )

        # If already WAV with correct format, return as-is
        if input_file.suffix.lower() == ".wav" and not output_path:
            return str(input_file)

        if not self.ffmpeg_available:
            # If no ffmpeg and file is WAV, use directly
            if input_file.suffix.lower() == ".wav":
                return str(input_file)
            raise TranscriptionError(
                "ffmpeg not available for audio conversion"
            )

        # Convert with ffmpeg
        if output_path is None:
            output_path = tempfile.mktemp(suffix=".wav")

        cmd = [
            "ffmpeg", "-y", "-i", str(input_file),
            "-ar", str(sample_rate),
            "-ac", "1",  # Mono
            "-f", "wav",
            output_path,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                raise TranscriptionError(
                    f"ffmpeg conversion failed: {result.stderr[-200:]}"
                )
            return output_path
        except subprocess.TimeoutExpired:
            raise TranscriptionError("ffmpeg conversion timed out")

    def get_duration(self, audio_path: str) -> float:
        """Get audio duration in seconds using ffprobe."""
        if not self.ffmpeg_available:
            return 0.0

        cmd = [
            "ffprobe", "-v", "quiet", "-show_entries",
            "format=duration", "-of", "csv=p=0", audio_path,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
            return float(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError):
            return 0.0

    def split_audio(self, audio_path: str, chunk_seconds: int = 300) -> list:
        """
        Split long audio into chunks for processing.

        Args:
            audio_path: Path to audio file.
            chunk_seconds: Maximum chunk duration (default: 5 minutes).

        Returns:
            List of paths to chunk files.
        """
        duration = self.get_duration(audio_path)
        if duration <= chunk_seconds:
            return [audio_path]

        chunks = []
        offset = 0
        chunk_num = 0

        while offset < duration:
            chunk_path = tempfile.mktemp(suffix=f"_chunk{chunk_num}.wav")
            cmd = [
                "ffmpeg", "-y", "-i", audio_path,
                "-ss", str(offset),
                "-t", str(chunk_seconds),
                "-ar", "16000", "-ac", "1", "-f", "wav",
                chunk_path,
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=30,
                )
                if result.returncode == 0:
                    chunks.append(chunk_path)
            except subprocess.TimeoutExpired:
                break

            offset += chunk_seconds
            chunk_num += 1

        return chunks if chunks else [audio_path]


class WhisperClient:
    """Client for OpenAI-compatible Whisper API (via DO Inference)."""

    def __init__(self, api_key: str = "", base_url: str = "",
                 model: str = "whisper-large-v3"):
        """
        Initialize Whisper client.

        Args:
            api_key: API key for authentication.
            base_url: Base URL for the API endpoint.
            model: Whisper model to use.
        """
        self.api_key = api_key or os.environ.get("DO_INFERENCE_API_KEY", "")
        self.base_url = base_url or os.environ.get(
            "DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"
        )
        self.model = model or os.environ.get("WHISPER_MODEL", "whisper-large-v3")

    def transcribe(self, audio_path: str, language: str = "en",
                   prompt: str = "") -> dict:
        """
        Transcribe audio file using Whisper API.

        Args:
            audio_path: Path to the audio file (WAV preferred).
            language: Language code (default: en).
            prompt: Optional prompt to guide transcription.

        Returns:
            Dict with 'text' key containing the transcription.
        """
        if not self.api_key:
            raise TranscriptionError("No API key configured for Whisper")

        audio_file = Path(audio_path)
        if not audio_file.exists():
            raise TranscriptionError(f"Audio file not found: {audio_path}")

        # Read audio file
        audio_data = audio_file.read_bytes()

        # Build multipart form data
        boundary = "----WebKitFormBoundary" + os.urandom(8).hex()
        body = self._build_multipart_body(
            boundary, audio_data, audio_file.name,
            language=language, model=self.model, prompt=prompt,
        )

        url = f"{self.base_url.rstrip('/')}/audio/transcriptions"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )

        # Retry with backoff
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    result = json.loads(resp.read())
                return result
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < 2:
                    wait = (2 ** attempt) * 2
                    print(
                        f"[transcribe] API {e.code}, retry in {wait}s",
                        flush=True,
                    )
                    time.sleep(wait)
                    continue
                error_body = e.read().decode(errors="replace")[:200]
                raise TranscriptionError(
                    f"Whisper API error {e.code}: {error_body}"
                )
            except urllib.error.URLError as e:
                raise TranscriptionError(f"Network error: {e.reason}")
            except Exception as e:
                raise TranscriptionError(f"Transcription failed: {e}")

        raise TranscriptionError("Max retries exceeded")

    def _build_multipart_body(self, boundary: str, audio_data: bytes,
                              filename: str, **fields) -> bytes:
        """Build multipart/form-data body for the API request."""
        parts = []

        # Add text fields
        for key, value in fields.items():
            if value:
                parts.append(
                    f"--{boundary}\r\n"
                    f"Content-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                    f"{value}\r\n"
                )

        # Add file field
        content_type = "audio/wav"
        if filename.endswith(".ogg"):
            content_type = "audio/ogg"
        elif filename.endswith(".mp3"):
            content_type = "audio/mpeg"
        elif filename.endswith(".flac"):
            content_type = "audio/flac"

        file_header = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        )

        # Assemble body
        body = "".join(parts).encode("utf-8")
        body += file_header.encode("utf-8")
        body += audio_data
        body += f"\r\n--{boundary}--\r\n".encode("utf-8")

        return body


class Transcriber:
    """
    Main transcription orchestrator.
    Handles the full pipeline: convert -> chunk -> transcribe -> merge.
    """

    def __init__(self, model: str = "whisper-large-v3", language: str = "en"):
        """
        Initialize the transcriber.

        Args:
            model: Whisper model to use.
            language: Default language for transcription.
        """
        self.converter = AudioConverter()
        self.client = WhisperClient(model=model)
        self.language = language
        self._temp_files: list = []

    def transcribe_file(self, audio_path: str, language: str = "",
                        prompt: str = "") -> str:
        """
        Transcribe an audio file to text.

        Full pipeline:
        1. Convert to WAV if needed
        2. Split into chunks if too long
        3. Transcribe each chunk
        4. Merge transcriptions

        Args:
            audio_path: Path to the audio file.
            language: Language override.
            prompt: Optional context prompt.

        Returns:
            Transcription text.
        """
        language = language or self.language
        print(f"[transcribe] Processing: {audio_path}", flush=True)

        try:
            # Step 1: Convert to WAV
            wav_path = self.converter.convert_to_wav(audio_path)
            if wav_path != audio_path:
                self._temp_files.append(wav_path)

            # Step 2: Check duration and split if needed
            duration = self.converter.get_duration(wav_path)
            if duration > 0:
                print(f"[transcribe] Duration: {duration:.1f}s", flush=True)

            if duration > 600:  # >10 minutes
                print("[transcribe] Long audio, splitting into chunks", flush=True)
                chunks = self.converter.split_audio(wav_path, chunk_seconds=300)
                self._temp_files.extend(
                    c for c in chunks if c != wav_path
                )
            else:
                chunks = [wav_path]

            # Step 3: Transcribe each chunk
            transcriptions = []
            for i, chunk in enumerate(chunks):
                if len(chunks) > 1:
                    print(
                        f"[transcribe] Chunk {i+1}/{len(chunks)}",
                        flush=True,
                    )
                result = self.client.transcribe(
                    chunk, language=language, prompt=prompt,
                )
                text = result.get("text", "").strip()
                if text:
                    transcriptions.append(text)

            # Step 4: Merge
            full_text = " ".join(transcriptions)
            print(
                f"[transcribe] Done: {len(full_text)} chars transcribed",
                flush=True,
            )
            return full_text

        finally:
            self._cleanup_temp_files()

    def _cleanup_temp_files(self):
        """Remove temporary files created during processing."""
        for f in self._temp_files:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass
        self._temp_files.clear()


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for voice transcription."""
    parser = argparse.ArgumentParser(
        description="Voice Transcription -- Rhodawk AI Hermes88"
    )
    parser.add_argument(
        "--audio-path", required=True,
        help="Path to audio file to transcribe"
    )
    parser.add_argument(
        "--model", default="whisper-large-v3",
        help="Whisper model to use (default: whisper-large-v3)"
    )
    parser.add_argument(
        "--language", default="en",
        help="Language code (default: en)"
    )
    parser.add_argument(
        "--prompt", default="",
        help="Optional context prompt to guide transcription"
    )
    parser.add_argument(
        "--output", default="",
        help="Output file path (default: stdout)"
    )
    args = parser.parse_args()

    transcriber = Transcriber(model=args.model, language=args.language)

    try:
        text = transcriber.transcribe_file(
            args.audio_path,
            language=args.language,
            prompt=args.prompt,
        )

        if args.output:
            Path(args.output).write_text(text)
            print(f"[transcribe] Saved to: {args.output}", flush=True)
        else:
            print(text)

    except TranscriptionError as e:
        print(f"[transcribe] ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"[transcribe] UNEXPECTED ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
