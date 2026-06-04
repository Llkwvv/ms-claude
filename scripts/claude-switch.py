#!/usr/bin/env python3
"""
Claude settings profile switcher.

Manage two local profiles:
- cc-switch
- ms-claude

The script keeps `~/.claude/settings.json` as the active file and stores
profiles in `~/.claude/profiles/`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


CLAUDE_DIR = Path.home() / ".claude"
ACTIVE_SETTINGS = CLAUDE_DIR / "settings.json"
PROFILE_DIR = CLAUDE_DIR / "profiles"
STATE_FILE = CLAUDE_DIR / ".ms-claude-switch-state.json"

PROFILE_FILES = {
    "cc-switch": PROFILE_DIR / "settings.cc-switch.json",
    "ms-claude": PROFILE_DIR / "settings.ms-claude.json",
}


@dataclass
class SwitchState:
    active_profile: Optional[str] = None
    updated_at: Optional[str] = None


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_state() -> SwitchState:
    if not STATE_FILE.exists():
        return SwitchState()
    try:
        data = load_json(STATE_FILE)
    except Exception:
        return SwitchState()
    return SwitchState(
        active_profile=data.get("active_profile"),
        updated_at=data.get("updated_at"),
    )


def write_state(active_profile: Optional[str]) -> None:
    write_json(STATE_FILE, {
        "active_profile": active_profile,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def profile_path(name: str) -> Path:
    if name not in PROFILE_FILES:
        raise SystemExit(f"Unknown profile: {name}")
    return PROFILE_FILES[name]


def ensure_active_exists() -> None:
    if not ACTIVE_SETTINGS.exists():
        raise SystemExit(f"Active Claude settings not found: {ACTIVE_SETTINGS}")


def save_profile(name: str, source: Optional[Path] = None) -> Path:
    source_path = source or ACTIVE_SETTINGS
    ensure_active_exists() if source_path == ACTIVE_SETTINGS else None
    if not source_path.exists():
        raise SystemExit(f"Source settings not found: {source_path}")

    target = profile_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target)
    return target


def derive_ms_settings(source: Dict[str, Any]) -> Dict[str, Any]:
    data = json.loads(json.dumps(source))
    env = data.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8080"
    env["ANTHROPIC_AUTH_TOKEN"] = env.get("ANTHROPIC_AUTH_TOKEN", "PROXY_MANAGED")
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)

    hooks = data.get("hooks", {})
    if isinstance(hooks, dict):
        hooks.pop("UserPromptSubmit", None)
        hooks.pop("Stop", None)
        if not hooks:
            data.pop("hooks", None)

    return data


def init_profiles() -> None:
    ensure_active_exists()
    current = load_json(ACTIVE_SETTINGS)
    save_profile("cc-switch", ACTIVE_SETTINGS)
    write_json(profile_path("ms-claude"), derive_ms_settings(current))
    write_state("cc-switch")
    print(f"Saved cc-switch profile: {profile_path('cc-switch')}")
    print(f"Saved ms-claude profile: {profile_path('ms-claude')}")
    print("Active profile set to: cc-switch")


def activate_profile(name: str) -> None:
    target = profile_path(name)
    if not target.exists():
        raise SystemExit(
            f"Profile not found: {target}\n"
            "Run `python3 scripts/claude-switch.py init` first."
        )

    state = read_state()
    if state.active_profile and state.active_profile in PROFILE_FILES:
        current_profile_path = profile_path(state.active_profile)
        if ACTIVE_SETTINGS.exists():
            save_profile(state.active_profile, ACTIVE_SETTINGS)

    shutil.copy2(target, ACTIVE_SETTINGS)
    write_state(name)
    print(f"Activated profile: {name}")
    print(f"Wrote: {ACTIVE_SETTINGS}")


def export_profile(name: str) -> None:
    ensure_active_exists()
    if name == "ms-claude":
        write_json(profile_path(name), derive_ms_settings(load_json(ACTIVE_SETTINGS)))
    else:
        save_profile(name, ACTIVE_SETTINGS)
    print(f"Exported active settings to: {profile_path(name)}")


def show_status() -> None:
    state = read_state()
    print(f"Active settings: {ACTIVE_SETTINGS}")
    print(f"Active profile: {state.active_profile or 'unknown'}")
    print(f"State file: {STATE_FILE}")
    for name, path in PROFILE_FILES.items():
        print(f"{name}: {'present' if path.exists() else 'missing'} ({path})")


def toggle_profile() -> None:
    state = read_state()
    if state.active_profile == "ms-claude":
        activate_profile("cc-switch")
    else:
        activate_profile("ms-claude")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Switch Claude settings between cc-switch and ms-claude profiles"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Snapshot current active settings into both profiles")

    use_parser = subparsers.add_parser("use", help="Activate a saved profile")
    use_parser.add_argument("profile", choices=sorted(PROFILE_FILES.keys()))

    save_parser = subparsers.add_parser("save", help="Save active settings into a profile")
    save_parser.add_argument("profile", choices=sorted(PROFILE_FILES.keys()))

    export_parser = subparsers.add_parser("export", help="Export active settings into a profile")
    export_parser.add_argument("profile", choices=sorted(PROFILE_FILES.keys()))

    subparsers.add_parser("toggle", help="Toggle between the two profiles")
    subparsers.add_parser("status", help="Show profile and state status")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init":
        init_profiles()
        return 0

    if args.command == "use":
        activate_profile(args.profile)
        return 0

    if args.command == "save":
        export_profile(args.profile)
        return 0

    if args.command == "export":
        export_profile(args.profile)
        return 0

    if args.command == "toggle":
        toggle_profile()
        return 0

    if args.command == "status":
        show_status()
        return 0

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
