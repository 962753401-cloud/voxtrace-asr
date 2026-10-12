# VoxTrace ASR

Speaker-aware speech transcription skill for Qwen Audio ASR, built around a two-stage confirmation workflow and timestamped transcript output.

## Features

- Speaker-separated, timestamped transcripts.
- Two-stage workflow: prepare the plan, confirm, then generate the transcription.
- Supports local audio files and public audio URLs.
- Emits both readable TXT and structured JSON.

## Setup

See the Chinese deployment guide for full installation and usage instructions: `部署安装说明.md`.

```bash
python -m pip install -r requirements.txt
export DASHSCOPE_API_KEY="<your-api-key>"
export DASHSCOPE_WORKSPACE_ID="<your-workspace-id>"
python scripts/asr_qwen.py --help
```

## Security

No real credentials are stored in this repository. Configure `DASHSCOPE_API_KEY` and `DASHSCOPE_WORKSPACE_ID` through environment variables only.
