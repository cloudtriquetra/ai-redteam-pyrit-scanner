# AI Red Team PyRIT Scanner

A CLI scanner wrapping [PyRIT](https://microsoft.github.io/PyRIT) for testing LLMs and hosted VLM/audio models. Designed to work alongside [ai-redteam-server](https://github.com/cloudtriquetra/ai-redteam-server) for end-to-end red teaming of vision and audio models.

---

## Features

- **Three target types** — Claude (Anthropic), OpenAI GPT, or any model hosted via ai-redteam-server
- **All PyRIT built-in scenarios** — jailbreak, content harms, cyber, leakage, psychosocial, Garak encoding
- **Chainable converters** — text (base64, leetspeak, rot13) for LLMs; image/audio for VLM and speech models
- **Configurable scoring** — LLM-free substring scoring by default; optional LLM-based scoring with OpenAI
- **Profile-driven** — save scan configs as YAML for repeatable ADO pipeline runs
- **Interactive mode** — guided setup for ad hoc exploration
- **JSON reports** — machine-readable output saved to `./reports/`

---

## Requirements

- Python 3.10+
- PyRIT 0.12.1
- Anthropic API key (for Claude targets)
- OpenAI API key (optional — only needed for OpenAI targets or LLM-based scoring)

---

## Installation

```bash
git clone https://github.com/cloudtriquetra/ai-redteam-pyrit-scanner
cd ai-redteam-pyrit-scanner

pip install -r requirements.txt

cp .env.example .env
# Edit .env and add your API keys
```

---

## Configuration — `.env`

```bash
# Required for Claude targets
ANTHROPIC_API_KEY=sk-ant-...

# Required for OpenAI targets OR LLM-based scoring
OPENAI_CHAT_KEY=sk-...
OPENAI_CHAT_ENDPOINT=https://api.openai.com/v1/chat/completions
OPENAI_CHAT_MODEL=gpt-4o-mini

# Required for ai-redteam-server (fastapi target)
REDTEAM_SERVER_URL=http://localhost:8000
```

---

## Quick Start

```bash
# See all available options
python3 scan.py --list

# Interactive guided mode
python3 scan.py --interactive

# Run from a saved profile
python3 scan.py --profile profiles/claude_haiku_jailbreak.yaml

# Run inline
python3 scan.py \
    --target claude \
    --model claude-haiku-4-5-20251001 \
    --scenario airt.jailbreak \
    --strategies prompt_sending \
    --max-prompts 5
```

---

## All Parameters

| Parameter        | Description                                   | Default                 |
| ---------------- | --------------------------------------------- | ----------------------- |
| `--target`       | `claude` \| `openai` \| `fastapi`             | required                |
| `--model`        | Model name (e.g. `claude-haiku-4-5-20251001`) | target default          |
| `--api-key`      | API key — overrides `.env`                    | from `.env`             |
| `--url`          | Server URL for fastapi target                 | `http://localhost:8000` |
| `--scenario`     | PyRIT scenario (e.g. `airt.jailbreak`)        | required                |
| `--strategies`   | Scenario strategies, space-separated          | scenario default        |
| `--converters`   | Converters to apply, chainable                | none                    |
| `--turns`        | `oneturn` \| `multiturn`                      | `oneturn`               |
| `--dataset`      | Override scenario default dataset             | scenario default        |
| `--max-prompts`  | Max prompts from dataset                      | `5`                     |
| `--scorer`       | `substring` \| `llm`                          | `substring`             |
| `--scorer-model` | Model for LLM scorer (e.g. `gpt-4o-mini`)     | `OPENAI_CHAT_MODEL`     |
| `--memory`       | `sqlite` \| `in_memory` \| `azure_sql`        | `sqlite`                |
| `--report-dir`   | Output directory for JSON reports             | `./reports`             |
| `--no-console`   | Suppress console output                       | false                   |

---

## Scenarios

| Scenario                 | What it tests                       | Strategies                                                               |
| ------------------------ | ----------------------------------- | ------------------------------------------------------------------------ |
| `airt.jailbreak`         | Safety guardrail bypass             | `prompt_sending`, `many_shot`, `skeleton`, `role_play`                   |
| `airt.content_harms`     | Harmful content generation          | `hate`, `fairness`, `violence`, `sexual`, `harassment`, `misinformation` |
| `airt.cyber`             | Cybersecurity harms                 | `single_turn`, `multi_turn`                                              |
| `airt.leakage`           | Data and information leakage        | `first_letter`, `image`, `role_play`, `crescendo`                        |
| `airt.psychosocial`      | Psychosocial harms                  | `imminent_crisis`, `licensed_therapist`                                  |
| `foundry.red_team_agent` | Multi-turn automated red team agent | —                                                                        |
| `garak.encoding`         | Encoding-based bypass               | `base64`, `rot13`, `unicode`                                             |

---

## Converters

Converters transform the prompt **before** it is sent to the target. They can be chained.

### Text converters — for LLM targets (claude, openai)

| Converter       | What it does                         |
| --------------- | ------------------------------------ |
| `base64`        | Encode prompt in Base64              |
| `rot13`         | ROT13 cipher                         |
| `binary`        | Convert to binary                    |
| `morse`         | Morse code                           |
| `leetspeak`     | Replace letters with numbers (h3ll0) |
| `unicode`       | Unicode confusable characters        |
| `random_caps`   | Random capitalisation                |
| `suffix_append` | Append suffix to prompt              |
| `url_encode`    | URL percent-encoding                 |
| `flip`          | Flip text characters                 |

### Multimodal converters — for fastapi targets

| Converter       | What it does                | Use for                  |
| --------------- | --------------------------- | ------------------------ |
| `text_to_image` | Render prompt as PNG image  | TrOCR, BLIP, LLaVA, VilT |
| `text_to_audio` | Synthesise prompt as speech | Whisper, Wav2Vec2        |

`text_to_audio` requires Azure Speech credentials in `.env`:

```bash
AZURE_SPEECH_KEY=your_key
AZURE_SPEECH_REGION=eastus
```

---

## Scoring

The scanner supports two scoring modes, configurable per run.

### Substring scoring (default — no LLM required)

Checks whether the model's response contains refusal keywords (`"I cannot"`). No API key needed for scoring. Suitable for quick scans and CI pipelines.

```bash
python3 scan.py --target claude --scenario airt.jailbreak --scorer substring
```

In a profile:

```yaml
scan:
  scorer: substring
```

**When to use:** Any scan where you don't have an OpenAI key available, or want fast LLM-free scoring.

**Limitation:** A fixed keyword doesn't catch all refusal patterns. A model that refuses with `"I'm unable to..."` instead of `"I cannot..."` will be scored as a successful jailbreak even though it refused.

### LLM-based scoring (optional — requires OpenAI)

Uses PyRIT's `SelfAskRefusalScorer` — sends each model response to an OpenAI model which judges whether it represents a refusal or a genuine harmful output. More accurate, slower, costs API credits.

```bash
# Uses OPENAI_CHAT_MODEL from .env as scorer
python3 scan.py --target claude --scenario airt.jailbreak --scorer llm

# Specify scorer model explicitly
python3 scan.py --target claude --scenario airt.jailbreak \
    --scorer llm --scorer-model gpt-4o-mini
```

In a profile:

```yaml
scan:
  scorer: llm
  scorer_model: gpt-4o-mini # optional — falls back to OPENAI_CHAT_MODEL in .env
```

Required `.env` entries for LLM scoring:

```bash
OPENAI_CHAT_KEY=sk-...
OPENAI_CHAT_ENDPOINT=https://api.openai.com/v1/chat/completions
OPENAI_CHAT_MODEL=gpt-4o-mini
```

**When to use:** When scoring accuracy matters — full red team campaigns, formal reporting, or when the target model may use varied refusal language.

---

## Profiles

Profiles are YAML files that save a full scan configuration. Use them for repeatable runs and ADO pipelines.

```yaml
# profiles/my_scan.yaml

name: My Scan
description: Short description shown in --list

target:
  type: claude # claude | openai | fastapi
  model: claude-haiku-4-5-20251001
  # api_key: override here or use .env
  # url: http://localhost:8000        # fastapi only

scan:
  scenario: airt.jailbreak
  strategies:
    - prompt_sending
    - many_shot
  converters:
    - base64
    - leetspeak
  turns: oneturn
  scorer: substring # substring | llm
  # scorer_model: gpt-4o-mini        # only needed when scorer: llm
  max_dataset_size: 5
  # dataset: harmbench               # override scenario default dataset

output:
  console: true
  json: true
  report_dir: ./reports
```

Run with:

```bash
python3 scan.py --profile profiles/my_scan.yaml
```

---

## Examples

```bash
# Claude Haiku jailbreak — default substring scorer
python3 scan.py \
    --target claude \
    --model claude-haiku-4-5-20251001 \
    --scenario airt.jailbreak \
    --strategies prompt_sending many_shot \
    --converters base64 leetspeak \
    --max-prompts 5

# Claude Haiku jailbreak — LLM scorer with GPT-4o-mini
python3 scan.py \
    --target claude \
    --model claude-haiku-4-5-20251001 \
    --scenario airt.jailbreak \
    --strategies prompt_sending \
    --scorer llm \
    --scorer-model gpt-4o-mini \
    --max-prompts 5

# TrOCR pipeline injection via image converter
python3 scan.py \
    --target fastapi \
    --url http://YOUR_EC2_IP:8000 \
    --model trocr-base-printed \
    --scenario airt.jailbreak \
    --converters text_to_image \
    --strategies prompt_sending \
    --max-prompts 5

# Whisper pipeline injection via audio converter
python3 scan.py \
    --target fastapi \
    --url http://YOUR_EC2_IP:8000 \
    --model whisper-base \
    --scenario airt.jailbreak \
    --converters text_to_audio \
    --strategies prompt_sending \
    --max-prompts 5

# OpenAI content harms — chainable converters
python3 scan.py \
    --target openai \
    --model gpt-4o \
    --scenario airt.content_harms \
    --strategies hate violence \
    --converters base64 leetspeak \
    --max-prompts 3
```

---

## Project Structure

```
ai-redteam-pyrit-scanner/
├── scan.py                              # Main CLI
├── requirements.txt
├── .env.example                         # Copy to .env and add keys
├── .gitignore
├── initializers/
│   └── claude_initializer.py            # Registers Claude into PyRIT TargetRegistry
├── profiles/                            # Saved scan configs
│   ├── claude_haiku_jailbreak.yaml
│   ├── claude_haiku_content_harms.yaml
│   ├── trocr_jailbreak.yaml
│   └── whisper_injection.yaml
└── reports/                             # JSON scan output (git-ignored)
```

---

## Related

- [ai-redteam-server](https://github.com/cloudtriquetra/ai-redteam-server) — Universal model hosting server (Project 1)
- [PyRIT documentation](https://microsoft.github.io/PyRIT) — Microsoft AI Red Team framework
- [CoPyRIT](https://microsoft.github.io/PyRIT/gui/gui) — PyRIT GUI for interactive red teaming
