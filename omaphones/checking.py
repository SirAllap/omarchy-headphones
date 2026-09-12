"""Shared author checks: registry, pure adapter boundary, evidence and replays."""
import ast
import importlib.util
import json
from pathlib import Path
import unittest

from omaphones.registry import ROOT, contained, descriptors, load_protocol, read_json
from omaphones.testing import Replay

ALLOWED_IMPORTS = {"omaphones.api", "struct", "enum", "dataclasses", "collections", "math", "typing", "re"}


def check_boundary(path):
    tree = ast.parse(path.read_text(), str(path))
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        if any(module not in ALLOWED_IMPORTS for module in modules):
            raise ValueError(str(path) + ": adapter imports outside the protocol API: " + ", ".join(modules))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"open", "exec", "eval", "__import__", "compile", "input", "print"}:
            raise ValueError(str(path) + ": platform operation belongs in the runtime: " + node.func.id)


def suite_for(adapter_id=None, root=ROOT, include_drafts=False):
    rows = descriptors(root, drafts=include_drafts)
    if adapter_id:
        rows = [row for row in rows if row['id'] == adapter_id]
        if not rows:
            raise ValueError("unknown or draft adapter: " + adapter_id)
    suite = unittest.TestSuite()
    for row in rows:
        package = root / "adapters" / row['id']
        if not row.get('entry'):
            continue
        check_boundary(contained(package, row['entry']))
        test_paths = sorted((package / 'tests').glob('*_test.py'))
        # Migrated adapters additionally replay the frozen legacy sessions in
        # tests/adapter_api_test.py, which tools/check runs unchanged alongside
        # the legacy bridges' own tests.
        if not test_paths and not row.get('legacy'):
            raise ValueError(row['id'] + ': no protocol tests')
        for path in test_paths:
            spec = importlib.util.spec_from_file_location('adapter_test_' + row['id'].replace('-', '_') + '_' + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    return suite
