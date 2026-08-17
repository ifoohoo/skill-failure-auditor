#!/usr/bin/env python3
"""从 kimi.plugin.json 机械生成 .kimi-plugin/plugin.json（字段完全相等，禁止双写）。"""
import argparse
import json
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = PACKAGE_ROOT / "plugin-src" / "platforms" / "kimi-code" / "kimi.plugin.json"
DEFAULT_OUT = PACKAGE_ROOT / "plugin-src" / "platforms" / "kimi-code" / ".kimi-plugin" / "plugin.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", action="store_true", help="只比较不写入")
    args = parser.parse_args()
    source = Path(args.source)
    out = Path(args.out)
    data = json.loads(source.read_text(encoding="utf-8"))
    canonical = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not out.is_file():
            print(json.dumps({"status": "FAIL", "reason": "MISSING_PROJECTION"}))
            return 1
        current = json.loads(out.read_text(encoding="utf-8"))
        equal = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) == canonical.rstrip("\n")
        print(json.dumps({"status": "EQUAL" if (equal and current == data) else "DRIFT"}, ensure_ascii=False))
        return 0 if (equal and current == data) else 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical, encoding="utf-8")
    print(json.dumps({"status": "GENERATED", "out": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
