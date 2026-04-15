"""
scan.py — AI Red Team PyRIT Scanner
=====================================
CLI wrapper around PyRIT for scanning LLMs and hosted VLM/audio models.

Usage:
    python3 scan.py --list
    python3 scan.py --profile profiles/claude_haiku_jailbreak.yaml
    python3 scan.py --interactive

    # Claude — text converters
    python3 scan.py \\
        --target claude --model claude-haiku-4-5-20251001 \\
        --scenario airt.jailbreak \\
        --strategies prompt_sending many_shot \\
        --converters base64 leetspeak \\
        --max-prompts 5

    # FastAPI OCR — multimodal converter
    python3 scan.py \\
        --target fastapi --url http://localhost:8000 --model trocr-base-printed \\
        --scenario airt.jailbreak --strategies prompt_sending \\
        --converters text_to_image

    # FastAPI Audio — audio converter
    python3 scan.py \\
        --target fastapi --url http://localhost:8000 --model whisper-base \\
        --scenario airt.jailbreak --strategies prompt_sending \\
        --converters text_to_audio

Exit codes:
    0 — scan completed
    1 — setup or execution error
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ── Load environment ───────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path.home() / ".pyrit" / ".env")

# ── Constants ──────────────────────────────────────────────────────────────────

SCENARIOS = {
    "airt.jailbreak": {
        "description": "Jailbreak attempts",
        "strategies":  ["prompt_sending", "many_shot", "skeleton", "role_play"],
    },
    "airt.content_harms": {
        "description": "Harmful content generation",
        "strategies":  ["hate", "fairness", "violence", "sexual", "harassment", "misinformation"],
    },
    "airt.cyber": {
        "description": "Cybersecurity harms",
        "strategies":  ["single_turn", "multi_turn"],
    },
    "airt.leakage": {
        "description": "Data and information leakage",
        "strategies":  ["first_letter", "image", "role_play", "crescendo"],
    },
    "airt.psychosocial": {
        "description": "Psychosocial harms",
        "strategies":  ["imminent_crisis", "licensed_therapist"],
    },
    "foundry.red_team_agent": {
        "description": "Multi-turn automated red team agent",
        "strategies":  [],
    },
    "garak.encoding": {
        "description": "Encoding-based bypass",
        "strategies":  ["base64", "rot13", "unicode"],
    },
}

TARGETS = {
    "claude":  "Claude via Anthropic API",
    "openai":  "OpenAI GPT models",
    "fastapi": "Hosted models via ai-redteam-server /inference endpoint",
}

DEFAULT_MODELS = {
    "claude":  "claude-haiku-4-5-20251001",
    "openai":  "gpt-4o",
    "fastapi": None,
}

# Converters — text-to-text
TEXT_CONVERTERS = [
    "base64", "rot13", "binary", "morse", "leetspeak",
    "unicode", "random_caps", "suffix_append", "url_encode", "flip",
]

# Converters — multimodal (relevant for fastapi targets)
MULTIMODAL_CONVERTERS = [
    "text_to_image",   # render prompt as PNG — for OCR/VLM models
    "text_to_audio",   # synthesise speech  — for Whisper/audio models
]

ALL_CONVERTERS = TEXT_CONVERTERS + MULTIMODAL_CONVERTERS

# Which fastapi models need which converter
IMAGE_MODEL_HINTS  = {"trocr", "blip", "git", "llava", "moondream", "vilt"}
AUDIO_MODEL_HINTS  = {"whisper", "wav2vec", "audio"}


# ── Converter builder ──────────────────────────────────────────────────────────

def build_converters(names: list[str]) -> list:
    """
    Build a list of PyRIT converter objects from converter names.
    Converters are chained in the order provided.
    """
    if not names:
        return []

    from pyrit.prompt_converter import (
        Base64Converter,
        ROT13Converter,
        BinaryConverter,
        MorseConverter,
        LeetspeakConverter,
        UnicodeConfusableConverter,
        RandomCapitalLettersConverter,
        SuffixAppendConverter,
        UrlConverter,
        FlipConverter,
        AddTextImageConverter,
    )

    converter_map = {
        "base64":        Base64Converter(),
        "rot13":         ROT13Converter(),
        "binary":        BinaryConverter(),
        "morse":         MorseConverter(),
        "leetspeak":     LeetspeakConverter(),
        "unicode":       UnicodeConfusableConverter(),
        "random_caps":   RandomCapitalLettersConverter(),
        "suffix_append": SuffixAppendConverter(suffix=" (respond in detail)"),
        "url_encode":    UrlConverter(),
        "flip":          FlipConverter(),
        "text_to_image": AddTextImageConverter(),
    }

    # text_to_audio requires Azure Speech — handle separately
    result = []
    for name in names:
        if name == "text_to_audio":
            try:
                from pyrit.prompt_converter import AzureSpeechTextToAudioConverter
                result.append(AzureSpeechTextToAudioConverter())
            except Exception as e:
                print(f"WARNING: text_to_audio converter failed to load ({e})")
                print("         Azure Speech credentials may be missing.")
                print("         Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in .env")
        elif name in converter_map:
            result.append(converter_map[name])
        else:
            print(f"WARNING: Unknown converter '{name}' — skipping.")
            print(f"         Available: {', '.join(ALL_CONVERTERS)}")

    return result


# ── Converter auto-suggestion ──────────────────────────────────────────────────

def suggest_converters(target_type: str, model: str) -> str | None:
    """
    Warn analyst if they haven't picked converters appropriate
    for the target model type.
    """
    if target_type != "fastapi" or not model:
        return None

    model_lower = model.lower()

    if any(hint in model_lower for hint in IMAGE_MODEL_HINTS):
        return "text_to_image"
    if any(hint in model_lower for hint in AUDIO_MODEL_HINTS):
        return "text_to_audio"

    return None


# ── Target builders ────────────────────────────────────────────────────────────

def build_claude_target(model: str, api_key: str = None):
    from pyrit.prompt_target import OpenAIChatTarget

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ERROR: Anthropic API key not set.")
        print("       Pass --api-key or set ANTHROPIC_API_KEY in .env")
        sys.exit(1)

    return OpenAIChatTarget(
        model_name=model,
        endpoint="https://api.anthropic.com/v1/",
        api_key=key,
        headers={"anthropic-version": "2023-06-01"},
        max_tokens=1024,
    )


def build_openai_target(model: str, api_key: str = None):
    from pyrit.prompt_target import OpenAIChatTarget

    key      = api_key or os.environ.get("OPENAI_CHAT_KEY") or os.environ.get("OPENAI_API_KEY")
    endpoint = os.environ.get("OPENAI_CHAT_ENDPOINT", "https://api.openai.com/v1/chat/completions")

    if not key:
        print("ERROR: OpenAI API key not set.")
        print("       Pass --api-key or set OPENAI_CHAT_KEY in .env")
        sys.exit(1)

    return OpenAIChatTarget(
        model_name=model,
        endpoint=endpoint,
        api_key=key,
        max_tokens=1024,
    )


def build_fastapi_target(model: str, url: str):
    from pyrit.prompt_target import HTTPTarget

    if not model:
        print("ERROR: --model is required for fastapi target.")
        print("       Example: --model trocr-base-printed")
        sys.exit(1)

    url = url or os.environ.get("REDTEAM_SERVER_URL", "http://localhost:8000")

    def request_fn(prompt: str) -> dict:
        return {
            "url":     f"{url}/inference",
            "method":  "POST",
            "headers": {"Content-Type": "application/json"},
            "body":    json.dumps({"model": model, "prompt": prompt}),
        }

    return HTTPTarget(
        http_request=request_fn,
        response_entry="output",
    )


def build_target(target_type: str, model: str, api_key: str = None, url: str = None):
    model = model or DEFAULT_MODELS.get(target_type)
    if target_type == "claude":
        return build_claude_target(model, api_key)
    elif target_type == "openai":
        return build_openai_target(model, api_key)
    elif target_type == "fastapi":
        return build_fastapi_target(model, url)
    else:
        print(f"ERROR: Unknown target '{target_type}'. Choose from: {', '.join(TARGETS)}")
        sys.exit(1)


# ── Scenario runner ────────────────────────────────────────────────────────────

async def run_scan(
    target_type:    str,
    model:          str,
    scenario:       str,
    strategies:     list[str],
    converters:     list[str],
    turns:          str,
    dataset:        str | None,
    max_prompts:    int,
    api_key:        str | None = None,
    url:            str | None = None,
    memory:         str = "sqlite",
    report_dir:     str = "./reports",
    console_output: bool = True,
):
    from pyrit.setup import initialize_pyrit_async, IN_MEMORY
    from pyrit.setup.initializers import LoadDefaultDatasets
    from pyrit.scenario import DatasetConfiguration

    # ── Converter auto-suggestion ──────────────────────────────────────────────
    suggested = suggest_converters(target_type, model)
    if suggested and not converters:
        print(f"\n⚠️  Warning: '{model}' is a {suggested.split('_')[1].upper()} model.")
        print(f"   Text converters (base64, leetspeak etc.) won't have effect.")
        print(f"   Suggested converter for this model: --converters {suggested}")
        answer = input("   Continue without a converter? [y/n]: ").strip().lower()
        if answer != "y":
            sys.exit(0)
    elif suggested and converters:
        # Check they haven't picked a text converter for an image/audio model
        wrong = [c for c in converters if c in TEXT_CONVERTERS]
        if wrong:
            print(f"\n⚠️  Warning: {wrong} are text converters — they won't have effect")
            print(f"   on '{model}' which is a {suggested.split('_')[1].upper()} model.")
            print(f"   Suggested: --converters {suggested}")
            answer = input("   Continue anyway? [y/n]: ").strip().lower()
            if answer != "y":
                sys.exit(0)

    # ── Initialise PyRIT ───────────────────────────────────────────────────────
    print(f"\n[scan] Initialising PyRIT (memory={memory}) ...")
    mem_type = IN_MEMORY if memory == "in_memory" else memory
    await initialize_pyrit_async(
        memory_db_type=mem_type,
        initializers=[LoadDefaultDatasets()],
    )

    # ── Build target and converters ────────────────────────────────────────────
    print(f"[scan] Target:      {target_type} / {model or 'default'}")
    print(f"[scan] Scenario:    {scenario}")
    print(f"[scan] Strategies:  {strategies or ['default']}")
    print(f"[scan] Converters:  {converters or ['none']}")
    print(f"[scan] Turns:       {turns}")
    print(f"[scan] Max prompts: {max_prompts}")

    objective_target = build_target(target_type, model, api_key, url)
    converter_list   = build_converters(converters)

    # ── Dataset config ─────────────────────────────────────────────────────────
    dataset_names  = [dataset] if dataset else None
    dataset_config = DatasetConfiguration(
        dataset_names=dataset_names,
        max_dataset_size=max_prompts,
    )

    # ── Run scenario ───────────────────────────────────────────────────────────
    sc = None

    if scenario == "airt.jailbreak":
        from pyrit.scenario.scenarios.airt import Jailbreak, JailbreakStrategy
        strat_map = {s.name.lower(): s for s in JailbreakStrategy}
        selected  = [strat_map[s] for s in strategies if s in strat_map] or None
        sc = Jailbreak()
        await sc.initialize_async(
            objective_target=objective_target,
            scenario_strategies=selected,
            dataset_config=dataset_config,
            converters=converter_list or None,
        )

    elif scenario == "airt.content_harms":
        from pyrit.scenario.scenarios.airt import ContentHarms, ContentHarmsStrategy
        strat_map = {s.name.lower(): s for s in ContentHarmsStrategy}
        selected  = [strat_map[s] for s in strategies if s in strat_map] or None
        sc = ContentHarms()
        await sc.initialize_async(
            objective_target=objective_target,
            scenario_strategies=selected,
            dataset_config=dataset_config,
            converters=converter_list or None,
        )

    elif scenario == "airt.cyber":
        from pyrit.scenario.scenarios.airt import Cyber, CyberStrategy
        strat_map = {s.name.lower(): s for s in CyberStrategy}
        selected  = [strat_map[s] for s in strategies if s in strat_map] or None
        sc = Cyber()
        await sc.initialize_async(
            objective_target=objective_target,
            scenario_strategies=selected,
            dataset_config=dataset_config,
            converters=converter_list or None,
        )

    elif scenario == "airt.leakage":
        from pyrit.scenario.scenarios.airt import Leakage, LeakageStrategy
        strat_map = {s.name.lower(): s for s in LeakageStrategy}
        selected  = [strat_map[s] for s in strategies if s in strat_map] or None
        sc = Leakage()
        await sc.initialize_async(
            objective_target=objective_target,
            scenario_strategies=selected,
            dataset_config=dataset_config,
            converters=converter_list or None,
        )

    elif scenario == "airt.psychosocial":
        from pyrit.scenario.scenarios.airt import Psychosocial, PsychosocialStrategy
        strat_map = {s.name.lower(): s for s in PsychosocialStrategy}
        selected  = [strat_map[s] for s in strategies if s in strat_map] or None
        sc = Psychosocial()
        await sc.initialize_async(
            objective_target=objective_target,
            scenario_strategies=selected,
            dataset_config=dataset_config,
            converters=converter_list or None,
        )

    elif scenario == "garak.encoding":
        from pyrit.scenario.scenarios.garak import Encoding
        sc = Encoding()
        await sc.initialize_async(
            objective_target=objective_target,
            scenario_strategies=strategies or None,
            dataset_config=dataset_config,
            converters=converter_list or None,
        )

    elif scenario == "foundry.red_team_agent":
        from pyrit.scenario.scenarios.foundry import RedTeamAgent
        sc = RedTeamAgent()
        await sc.initialize_async(
            objective_target=objective_target,
            dataset_config=dataset_config,
            converters=converter_list or None,
        )

    else:
        print(f"ERROR: Scenario '{scenario}' not supported.")
        print(f"       Supported: {', '.join(SCENARIOS.keys())}")
        sys.exit(1)

    result = await sc.run_async()

    # ── Console output ─────────────────────────────────────────────────────────
    if console_output:
        from pyrit.scenario.printer.console_printer import ConsoleScenarioResultPrinter
        printer = ConsoleScenarioResultPrinter()
        await printer.print_summary_async(result)

    # ── JSON report ────────────────────────────────────────────────────────────
    try:
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_name = f"{target_type}_{scenario.replace('.', '_')}_{ts}.json"
        report_path = Path(report_dir) / report_name

        result_dict = result.to_dict() if hasattr(result, "to_dict") else {"result": str(result)}
        result_dict["meta"] = {
            "target":     target_type,
            "model":      model,
            "scenario":   scenario,
            "strategies": strategies,
            "converters": converters,
            "turns":      turns,
            "max_prompts": max_prompts,
            "timestamp":  datetime.now().isoformat(),
        }

        with open(report_path, "w") as f:
            json.dump(result_dict, f, indent=2, default=str)

        print(f"\n[scan] Report saved → {report_path}")

    except Exception as e:
        print(f"[scan] Warning: could not save JSON report: {e}")


# ── --list ─────────────────────────────────────────────────────────────────────

def print_list():
    print("\n╔═══════════════════════════════════════════════════════════════╗")
    print("║          AI Red Team PyRIT Scanner — Available Options       ║")
    print("╚═══════════════════════════════════════════════════════════════╝\n")

    print("TARGETS  (--target)")
    print("─" * 64)
    for name, desc in TARGETS.items():
        model = DEFAULT_MODELS.get(name)
        print(f"  {name:<10} {desc}")
        if model:
            print(f"             Default model: {model}")
    print()

    print("SCENARIOS  (--scenario)")
    print("─" * 64)
    for name, info in SCENARIOS.items():
        print(f"  {name}")
        print(f"             {info['description']}")
        if info["strategies"]:
            print(f"             Strategies: {', '.join(info['strategies'])}")
        print()

    print("CONVERTERS  (--converters, chainable)")
    print("─" * 64)
    print("  Text converters — for LLM targets (claude, openai)")
    for c in TEXT_CONVERTERS:
        print(f"    {c}")
    print()
    print("  Multimodal converters — for fastapi targets")
    print("    text_to_image    Render prompt as PNG image  ← for OCR/VLM models")
    print("    text_to_audio    Synthesise as speech        ← for Whisper/audio models")
    print()

    print("PROFILES  (--profile) — saved scan configs")
    print("─" * 64)
    profiles_dir = Path(__file__).parent / "profiles"
    if profiles_dir.exists():
        for p in sorted(profiles_dir.glob("*.yaml")):
            try:
                with open(p) as f:
                    data = yaml.safe_load(f)
                print(f"  {p.name:<44} {data.get('description', '')}")
            except Exception:
                print(f"  {p.name}")
    else:
        print("  No profiles found in ./profiles/")
    print()


# ── --interactive ──────────────────────────────────────────────────────────────

def interactive_mode() -> dict:
    print("\n╔═══════════════════════════════════════════════════════════════╗")
    print("║         AI Red Team PyRIT Scanner — Interactive Mode         ║")
    print("╚═══════════════════════════════════════════════════════════════╝\n")

    # Target
    print(f"Targets: {', '.join(TARGETS.keys())}")
    target = input("Target [claude]: ").strip() or "claude"

    # Model
    default_model = DEFAULT_MODELS.get(target, "")
    prompt_str    = f"Model [{default_model}]: " if default_model else "Model: "
    model         = input(prompt_str).strip() or default_model

    # API key
    api_key = None
    if target in ("claude", "openai"):
        api_key = input("API key (blank = use .env): ").strip() or None

    # URL (fastapi only)
    url = None
    if target == "fastapi":
        url = input("Server URL [http://localhost:8000]: ").strip() or "http://localhost:8000"

    # Scenario
    print(f"\nScenarios: {', '.join(SCENARIOS.keys())}")
    scenario = input("Scenario [airt.jailbreak]: ").strip() or "airt.jailbreak"

    # Strategies
    if scenario in SCENARIOS and SCENARIOS[scenario]["strategies"]:
        print(f"Strategies: {', '.join(SCENARIOS[scenario]['strategies'])}")
        strat_input = input("Strategies (space-separated, blank=default): ").strip()
        strategies  = strat_input.split() if strat_input else []
    else:
        strategies = []

    # Converters
    suggested = suggest_converters(target, model)
    if suggested:
        print(f"\nSuggested converter for '{model}': {suggested}")
    print(f"All converters: {', '.join(ALL_CONVERTERS)}")
    conv_input = input("Converters (space-separated, blank=none): ").strip()
    converters = conv_input.split() if conv_input else []

    # Turns
    turns_input = input("Turns [oneturn]: ").strip() or "oneturn"

    # Dataset override
    dataset_input = input("Dataset override (blank=scenario default): ").strip()
    dataset = dataset_input or None

    # Max prompts
    mp_input    = input("Max prompts [5]: ").strip()
    max_prompts = int(mp_input) if mp_input.isdigit() else 5

    return {
        "target":      target,
        "model":       model,
        "api_key":     api_key,
        "url":         url,
        "scenario":    scenario,
        "strategies":  strategies,
        "converters":  converters,
        "turns":       turns_input,
        "dataset":     dataset,
        "max_prompts": max_prompts,
    }


# ── Argument parser ────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        prog="scan.py",
        description="AI Red Team PyRIT Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scan.py --list
  python3 scan.py --interactive
  python3 scan.py --profile profiles/claude_haiku_jailbreak.yaml

  # Claude — text converters
  python3 scan.py --target claude --model claude-haiku-4-5-20251001 \\
      --scenario airt.jailbreak --strategies prompt_sending many_shot \\
      --converters base64 leetspeak --max-prompts 5

  # FastAPI OCR — image converter
  python3 scan.py --target fastapi --url http://localhost:8000 \\
      --model trocr-base-printed --scenario airt.jailbreak \\
      --converters text_to_image --strategies prompt_sending

  # FastAPI audio — audio converter
  python3 scan.py --target fastapi --url http://localhost:8000 \\
      --model whisper-base --scenario airt.jailbreak \\
      --converters text_to_audio --strategies prompt_sending

  # OpenAI — chainable text converters
  python3 scan.py --target openai --model gpt-4o \\
      --api-key $OPENAI_API_KEY --scenario airt.jailbreak \\
      --converters base64 leetspeak --strategies many_shot
        """
    )

    parser.add_argument("--list",        action="store_true", help="List all targets, scenarios, converters, profiles")
    parser.add_argument("--interactive", action="store_true", help="Guided interactive mode")
    parser.add_argument("--profile",     type=str,            help="Path to YAML profile file")

    # Target
    parser.add_argument("--target",      type=str,            help="Target type: claude | openai | fastapi")
    parser.add_argument("--model",       type=str,            help="Model name (e.g. claude-haiku-4-5-20251001)")
    parser.add_argument("--api-key",     type=str,            help="API key for claude or openai (overrides .env)")
    parser.add_argument("--url",         type=str,            help="Server URL for fastapi target")

    # Scan config
    parser.add_argument("--scenario",    type=str,            help="PyRIT scenario (e.g. airt.jailbreak)")
    parser.add_argument("--strategies",  type=str, nargs="+", help="Scenario strategies (e.g. prompt_sending many_shot)")
    parser.add_argument("--converters",  type=str, nargs="+", help="Converters to apply (chainable, e.g. base64 leetspeak)")
    parser.add_argument("--turns",       type=str, default="oneturn", choices=["oneturn", "multiturn"],
                        help="Attack turn mode (default: oneturn)")
    parser.add_argument("--dataset",     type=str,            help="Override scenario default dataset name")
    parser.add_argument("--max-prompts", type=int, default=5, help="Max prompts from dataset (default: 5)")

    # Output
    parser.add_argument("--memory",      type=str, default="sqlite",
                        choices=["sqlite", "in_memory", "azure_sql"],
                        help="PyRIT memory backend (default: sqlite)")
    parser.add_argument("--report-dir",  type=str, default="./reports",
                        help="Directory for JSON reports (default: ./reports)")
    parser.add_argument("--no-console",  action="store_true", help="Suppress console output")

    return parser.parse_args()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.list:
        print_list()
        sys.exit(0)

    if args.interactive:
        config = interactive_mode()

    elif args.profile:
        raw    = load_profile(args.profile)
        config = {
            "target":      raw["target"]["type"],
            "model":       raw["target"].get("model"),
            "api_key":     raw["target"].get("api_key"),
            "url":         raw["target"].get("url"),
            "scenario":    raw["scan"]["scenario"],
            "strategies":  raw["scan"].get("strategies", []),
            "converters":  raw["scan"].get("converters", []),
            "turns":       raw["scan"].get("turns", "oneturn"),
            "dataset":     raw["scan"].get("dataset"),
            "max_prompts": raw["scan"].get("max_dataset_size", 5),
        }
        print(f"\n[scan] Profile:     {args.profile}")
        print(f"[scan] Name:        {raw.get('name', 'unnamed')}")
        print(f"[scan] Description: {raw.get('description', '')}")

    elif args.target and args.scenario:
        config = {
            "target":      args.target,
            "model":       args.model,
            "api_key":     args.api_key,
            "url":         args.url,
            "scenario":    args.scenario,
            "strategies":  args.strategies or [],
            "converters":  args.converters or [],
            "turns":       args.turns,
            "dataset":     args.dataset,
            "max_prompts": args.max_prompts,
        }

    else:
        print("ERROR: Provide --list, --interactive, --profile, or --target + --scenario")
        print("       Run with --help for usage examples.")
        sys.exit(1)

    try:
        asyncio.run(run_scan(
            target_type    = config["target"],
            model          = config.get("model"),
            scenario       = config["scenario"],
            strategies     = config.get("strategies", []),
            converters     = config.get("converters", []),
            turns          = config.get("turns", "oneturn"),
            dataset        = config.get("dataset"),
            max_prompts    = config.get("max_prompts", 5),
            api_key        = config.get("api_key"),
            url            = config.get("url"),
            memory         = args.memory if hasattr(args, "memory") else "sqlite",
            report_dir     = args.report_dir if hasattr(args, "report_dir") else "./reports",
            console_output = not args.no_console if hasattr(args, "no_console") else True,
        ))
        sys.exit(0)

    except KeyboardInterrupt:
        print("\n[scan] Interrupted.")
        sys.exit(1)
    except Exception as e:
        print(f"\n[scan] ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def load_profile(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: Profile not found: {path}")
        sys.exit(1)
    with open(p) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    main()
