#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check Python 3.12 and required packages (fast, no Kokoro model load)."""
import importlib.util
import sys


def _pkg_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def main() -> int:
    print("  Python ...", flush=True)
    vi = sys.version_info
    if vi.major != 3 or vi.minor != 12:
        print(f"[Error] Need Python 3.12, got {vi.major}.{vi.minor}.{vi.micro}")
        print("        Run: py -3.12 check_env.py")
        return 1

    required = (
        ("kokoro", "0.9"),
        ("misaki", None),
        ("soundfile", None),
        ("pydub", None),
        ("edge_tts", None),
        ("google.genai", None),
    )
    missing: list[str] = []
    for mod, min_ver in required:
        print(f"  {mod} ...", flush=True)
        spec = importlib.util.find_spec(mod)
        if spec is None:
            missing.append(mod)
            continue
        if min_ver and mod == "kokoro":
            ver = _pkg_version("kokoro") or "0.0.0"
            parts = ver.split(".")
            try:
                major, minor = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                major, minor = 0, 0
            if (major, minor) < (0, 9):
                print(f"[Error] Need kokoro>=0.9, got {ver}")
                return 3

    if missing:
        print(f"[Error] Missing package(s): {', '.join(missing)}")
        print("        Run: py -3.12 -m pip install -r requirements.txt")
        return 2

    kv = _pkg_version("kokoro") or "?"
    print(f"[OK] Python {vi.major}.{vi.minor}  kokoro {kv}  (models load when dubbing)")

    print("  gemini_api_key (env) ...", flush=True)
    try:
        from gemini_secrets import gemini_api_key_from_env, gemini_api_key_status
        if gemini_api_key_from_env():
            print(f"[OK] {gemini_api_key_status()}")
        else:
            print("[Warn] GEMINI_API_KEY not set — see components\\GEMINI_API_KEY_SETUP.txt")
    except Exception as e:
        print(f"[Warn] gemini_secrets check failed: {e}")

    print("  pipeline_requirements ...", flush=True)
    try:
        from pipeline_requirements_check import verify_all_requirements
        req = verify_all_requirements(verbose=True)
        if not req.ok:
            print("[Warn] 用户需求对照自检有未通过项（见上方列表）")
    except Exception as e:
        print(f"[Warn] pipeline_requirements_check failed: {e}")

    print("  gemini_preflight ...", flush=True)
    try:
        from gemini_preflight import audit_api_payload, preflight_rewrite_plan
        bad, errs = audit_api_payload(
            [{'index': 0, 'zh': '测试', 'en': 'hello'}],
            'source_translate', block_on_error=False)
        if not bad or not errs:
            print("[Warn] gemini_preflight self-test: expected zh rejection")
        pf = preflight_rewrite_plan(
            [], {'translate_mode': 'skip', 'total_zh': 0, 'with_source': 0},
            cfg={'gemini_preflight_block': True})
        if not pf.get('blocked'):
            print("[Warn] gemini_preflight self-test: empty plan should block")
        else:
            print("[OK] gemini_preflight self-test passed")
    except Exception as e:
        print(f"[Warn] gemini_preflight self-test failed: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
