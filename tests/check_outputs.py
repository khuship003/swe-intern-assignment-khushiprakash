"""Run: python tests/check_outputs.py
1. INV-01 output must equal INV-01_expected_output.json
2. every outputs/*.json must satisfy output_schema.json  (uses `jsonschema` if installed,
   otherwise a small built-in structural check of required keys / types / enums)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
schema = json.loads((ROOT / "output_schema.json").read_text(encoding="utf-8"))


def builtin_check(doc, sch, path="$"):
    errs = []
    t = sch.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "null": type(None)}
    if t:
        allowed = [t] if isinstance(t, str) else t
        if not any(isinstance(doc, types[a]) and not (a in ("integer", "number") and isinstance(doc, bool)) for a in allowed):
            return [f"{path}: {type(doc).__name__} not in {allowed}"]
    if "enum" in sch and doc not in sch["enum"]:
        errs.append(f"{path}: {doc!r} not in {sch['enum']}")
    if "const" in sch and doc != sch["const"]:
        errs.append(f"{path}: {doc!r} != {sch['const']!r}")
    if isinstance(doc, dict):
        for k in sch.get("required", []):
            if k not in doc:
                errs.append(f"{path}: missing '{k}'")
        for k, sub in sch.get("properties", {}).items():
            if k in doc:
                errs += builtin_check(doc[k], sub, f"{path}.{k}")
    if isinstance(doc, list) and "items" in sch:
        for i, it in enumerate(doc):
            errs += builtin_check(it, sch["items"], f"{path}[{i}]")
    if sch.get("format") == "date" and isinstance(doc, str):
        from datetime import date
        try:
            date.fromisoformat(doc)
        except ValueError:
            errs.append(f"{path}: invalid date {doc!r}")
    return errs


def main() -> int:
    bad = 0
    for f in sorted((ROOT / "outputs").glob("*.json")):
        if f.name.endswith(".notes.json"):
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        try:
            import jsonschema
            jsonschema.validate(doc, schema)
            errs = []
        except ImportError:
            errs = builtin_check(doc, schema)
        except Exception as e:  # jsonschema.ValidationError
            errs = [str(e).splitlines()[0]]
        print(("OK   " if not errs else "FAIL ") + f.name, *errs, sep="\n     " if errs else "")
        bad += bool(errs)
    exp = json.loads((ROOT / "INV-01_expected_output.json").read_text(encoding="utf-8"))
    got = json.loads((ROOT / "outputs" / "INV-01_RE-2026-10418.json").read_text(encoding="utf-8"))
    print("INV-01 == expected:", exp == got)
    bad += exp != got
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
