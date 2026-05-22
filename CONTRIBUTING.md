# Contributing to opendesk-service

Thank you for your interest in contributing. This project powers a voice + vision backend for ESP32 desk robots (ASR → LLM → TTS + face animation protocol).

## Development setup

1. Clone the repository and copy environment templates:

   ```bash
   cp deskbot-server/.env.example deskbot-server/.env
   ```

2. Fill in `LLM_API_KEY` (DashScope OpenAI-compatible API key).

3. Download models (see root [README.md](README.md)).

4. Start the stack from the repository root:

   ```bash
   chmod +x start.sh
   ./start.sh
   ```

## Code layout

- `deskbot-server/src/deskbot_server/` — main Python package (src layout)
- `deskbot-server/data/` — bundled pb animation/scene JSON (required at runtime)
- `paddlespeech-server/src/paddlespeech_server/` — TTS sidecar (phoneme WS extension)
- `docs/` — protocol specs ([docs/README.md](docs/README.md))

## Pull requests

1. **One concern per PR** when possible (bugfix, feature, or refactor — not all three).
2. **Run checks** before opening a PR:

   ```bash
   cd deskbot-server
   source .venv/bin/activate
   pip install -e ".[dev]"
   ruff check src
   python -m compileall -q src

   cd ../paddlespeech-server
   pip install ruff pytest numpy
   ruff check src tests
   python -m compileall -q src
   PYTHONPATH=src pytest tests/ -q
   ```

3. **Do not commit** secrets (`.env`), model weights (`deskbot-server/models/`), or internal deployment scripts.
4. **Update docs** if you change WebSocket protocols, config keys, or startup steps.

## Commit messages

Use clear, imperative subjects (English or 中文均可), e.g.:

- `fix: import asyncio in pipeline.asr`
- `docs: clarify ASR model download steps`

## Reporting issues

Include: OS, Python version, how you started services (`./start.sh`), relevant log excerpts, and steps to reproduce.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
