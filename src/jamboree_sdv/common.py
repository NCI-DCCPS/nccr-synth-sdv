"""Strict JSON, hashing and experiment-local output boundaries."""
from datetime import datetime, timezone
import ast
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sys

EXPERIMENT = Path(__file__).resolve().parents[2]
WORKSPACE = EXPERIMENT.parent.parent
WORK = EXPERIMENT / "work"


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"Duplicate JSON key: {key}")
        out[key] = value
    return out


def read_json(path):
    def bad_constant(value):
        raise ValueError(f"Nonfinite JSON constant: {value}")
    def finite_float(value):
        parsed=float(value)
        if not math.isfinite(parsed):raise ValueError('Nonfinite JSON number')
        return parsed
    return json.loads(Path(path).read_bytes(), object_pairs_hook=_pairs,
                      parse_constant=bad_constant,parse_float=finite_float)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write("\n")


def output_path(path, fresh=True):
    original = Path(path)
    resolved = original.resolve()
    boundary = WORK.resolve()
    if boundary != EXPERIMENT.resolve() / 'work':
        raise ValueError('The work directory may not redirect outside the experiment')
    if not resolved.is_relative_to(boundary) or resolved == boundary:
        raise ValueError(f"Output must be below {boundary}")
    if fresh and (original.exists() or original.is_symlink()):
        raise ValueError(f"Refusing existing output: {original}")
    return resolved


def pin(path):
    p = Path(path).resolve()
    return {"path": str(p.relative_to(WORKSPACE)), "sha256": digest(p)}


def checked_pin(item):
    if set(item) != {"path", "sha256"}:
        raise ValueError("A file pin needs exactly path and sha256")
    p = (WORKSPACE / item["path"]).resolve()
    if not p.is_relative_to(WORKSPACE) or not p.is_file():
        raise ValueError(f"Input outside workspace or missing: {item['path']}")
    if digest(p) != item["sha256"]:
        raise ValueError(f"Input hash mismatch: {item['path']}")
    return p


def environment():
    return {"python": sys.version, "packages": {n: importlib.metadata.version(n)
            for n in ("jamboree-sdv", "copulas", "numpy", "pandas", "scipy")}}


def _shared_dependencies(directory, entrypoints):
    """Follow local Python imports without loading or executing shared tools."""
    pending = [directory / (name + '.py') for name in entrypoints]
    selected = set()
    while pending:
        path = pending.pop()
        if path in selected:
            continue
        selected.add(path)
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for name in names:
                dependency = directory / (name.split('.')[0] + '.py')
                if dependency.is_file() and dependency not in selected:
                    pending.append(dependency)
    return sorted(selected)


def code_pins(shared_entrypoints=None):
    paths = list((EXPERIMENT / "src").rglob("*.py"))
    paths += list((EXPERIMENT / "scripts").rglob("*.py"))
    paths += list((EXPERIMENT / "verifier").glob("check_raw_layout.py"))
    paths += [EXPERIMENT / x for x in ("pyproject.toml", "uv.lock")]
    directory = WORKSPACE / 'tools'
    paths += (sorted(directory.glob('*.py')) if shared_entrypoints is None
              else _shared_dependencies(directory, shared_entrypoints))
    return [pin(p) for p in sorted(paths)]


def shared_tools():
    path = str(WORKSPACE / "tools")
    if path not in sys.path:
        sys.path.insert(0, path)


def local_verifiers():
    shared_tools()
    path=str(EXPERIMENT/'verifier')
    if path not in sys.path:sys.path.insert(0,path)


def full_code_pins(integrated=False):
    pins=code_pins(['audit_all_source_pilot','assess_all_source_pilot',
                    'assess_scaled_candidate','candidate_evidence'])
    if integrated:
        pins += [pin(p) for p in sorted((EXPERIMENT/'verifier').glob('*.py'))]
    return sorted({p['path']:p for p in pins}.values(),key=lambda p:p['path'])

