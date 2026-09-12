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
        model_paths = sorted((package / 'models').glob('*.json'))
        if row['status'] == 'active' and not row.get('legacy') and not any(read_json(p).get('status', 'active') == 'active' for p in model_paths):
            raise ValueError(row['id'] + ': active adapter needs an evidenced model')
        for path in model_paths:
            model = read_json(path)
            if model.get('status') == 'draft':
                continue
            for reference in model.get('pins', []):
                pinpath = contained(root, reference)
                pin = read_json(pinpath)
                if pin.get('bridge'):
                    if not row.get('legacy') or pin['bridge'] != row['legacy']['bridge']:
                        raise ValueError('pin belongs to a different bridge: ' + reference)
                    continue
                if pin.get('apiVersion') != 1 or pin.get('adapter') != row['id']:
                    raise ValueError('wrong API or adapter in pin: ' + reference)
                if pin.get('owner') not in model['owners'] or pin.get('capture') not in model['captures']:
                    raise ValueError('pin must name this model owner and capture: ' + reference)
                steps = pin.get('steps', [])
                if not any('device' in s for s in steps) or not any('sent' in s for s in steps) or not any('values' in s or 'reports' in s for s in steps):
                    raise ValueError('pin needs device input, exact writes and observed state assertions')
                context = pin.get('context', {})
                from omaphones.registry import model_parameters
                if model_parameters(row, context, root) != model['parameters']:
                    raise ValueError('pin context does not select its model: ' + reference)
                def replay(pin=pin, row=row, context=context):
                    Replay(load_protocol(row, context, root)).play(unittest.TestCase(), pin)
                suite.addTest(unittest.FunctionTestCase(replay, description=reference))
    return suite
