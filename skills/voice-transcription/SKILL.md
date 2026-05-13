# Skill: voice-transcription (Peak v1.0)

## Purpose
Transcribe voice messages from Telegram (and other channels) using OpenAI Whisper
or DO Inference speech-to-text endpoint.

## When This Skill Applies
- Telegram voice message received
- Telegram audio file received
- Operator says "transcribe [URL]"
- Audio file attached to any channel message

## Invocation
python3 /app/skills/voice-transcription/transcribe.py \
  --audio-path /tmp/voice_msg.ogg \
  --model whisper-large-v3 \
  --language en

## Dependencies
- ffmpeg (installed in Dockerfile)
- openai or local whisper model via DO Inference

## Output
Plain text transcription, fed back into the conversation as operator input.

## Integration with Gateway
In gateway/run.py, voice messages are intercepted:
1. Download .ogg file from Telegram
2. Call transcribe.py
3. Feed resulting text as if operator typed it
4. Process normally (tool calls, responses, etc.)

## Environment Variables
- DO_INFERENCE_API_KEY: API key for Whisper endpoint
- DO_INFERENCE_BASE_URL: Base URL for the inference API
- WHISPER_MODEL: Model to use (default: whisper-large-v3)

## Supported Audio Formats
- OGG/Opus (Telegram voice messages)
- MP3
- WAV
- M4A
- FLAC
- WebM

## Error Handling
- If transcription fails: retry once with lower quality settings
- If audio is too long (>10min): split into chunks and transcribe each
- If format unsupported: attempt ffmpeg conversion first
