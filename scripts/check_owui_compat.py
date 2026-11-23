"""Simulate Open WebUI's tool load path against plugins/websearch.py.

Mirrors: extract_frontmatter, replace_imports, exec into a module, Tools class
lookup, Valves(**saved) instantiation, and spec derivation (public methods,
docstring description, :param: fields, type hints).
"""
import inspect
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
content = (ROOT / 'plugins' / 'websearch.py').read_text()

# --- replace_imports (utils/plugin.py) ---
replacements = {
    'from utils': 'from open_webui.utils',
    'from apps': 'from open_webui.apps',
    'from main': 'from open_webui.main',
    'from config': 'from open_webui.config',
}
for old, new in replacements.items():
    content = content.replace(old, new)

# --- extract_frontmatter (utils/plugin.py) ---
frontmatter = {}
frontmatter_started = False
frontmatter_ended = False
frontmatter_pattern = re.compile(r'^\s*([a-z_]+):\s*(.*)\s*$', re.IGNORECASE)
lines = content.splitlines()
assert lines[0].strip() == '"""', 'content must start with a docstring'
frontmatter_started = True
for line in lines[1:]:
    if '"""' in line:
        if frontmatter_started:
            frontmatter_ended = True
            break
    if frontmatter_started and not frontmatter_ended:
        match = frontmatter_pattern.match(line)
        if match:
            key, value = match.groups()
            frontmatter[key.strip()] = value.strip()
print('frontmatter:', frontmatter)
assert frontmatter.get('name') == 'WebSearch'

# --- exec into a module (utils/plugin.py) ---
module = types.ModuleType('tool_websearch_check')
exec(content, module.__dict__)
assert hasattr(module, 'Tools'), 'No Tools class found in the module'
tool = module.Tools()

# --- Valves(**saved) exactly as get_tools does it ---
module.valves = module.Valves()
module.valves = module.Valves(**{'SELECTION': 'exa', 'NUM_RESULTS': 5})
assert module.valves.SELECTION == 'exa' and module.valves.NUM_RESULTS == 5
module.valves = module.Valves(**{'BOGUS_KEY': 1})  # unknown keys must be ignored
assert module.valves.SELECTION == 'random'
print('Valves(**saved) OK (defaults, overrides, unknown keys ignored)')

# --- spec derivation (utils/tools.py: get_functions_from_tool + convert_*) ---
functions = [
    getattr(tool, func)
    for func in dir(tool)
    if callable(getattr(tool, func)) and not func.startswith('_') and not inspect.isclass(getattr(tool, func))
]
specs = []
for function in functions:
    sig = inspect.signature(function)
    params = {name: p for name, p in sig.parameters.items() if not name.startswith('__')}
    hints = {name: p.annotation for name, p in params.items() if p.annotation is not inspect.Parameter.empty}
    doc = function.__doc__ or ''
    description_lines = []
    param_docs = {}
    for line in doc.strip().split('\n'):
        line = line.strip()
        if re.match(r':param', line):
            m = re.match(r':param (\w+): (.*)', line)
            if m:
                param_docs[m.group(1)] = m.group(2)
            break
        description_lines.append(line)
    spec = {
        'name': function.__name__,
        'description': '\n'.join(description_lines),
        'parameters': {
            'type': 'object',
            'properties': {
                name: {
                    'type': 'string' if hints.get(name) is str else 'integer',
                    'description': param_docs.get(name, ''),
                }
                for name in params
            },
            'required': [name for name in params if params[name].default is inspect.Parameter.empty],
        },
    }
    specs.append(spec)
    print('spec:', spec['name'], '| required:', spec['parameters']['required'], '| desc len:', len(spec['description']))

assert {s['name'] for s in specs} == {'web_search', 'fetch_url'}, f'unexpected tools: {[s["name"] for s in specs]}'
web_search_spec = next(s for s in specs if s['name'] == 'web_search')
assert web_search_spec['parameters']['required'] == ['query']
assert 'The current year is' in web_search_spec['description']
assert 'query' in web_search_spec['parameters']['properties'] and 'description' in web_search_spec['parameters']['properties']['query']

print()
print('OWUI load path simulation: OK')
print('spec description for web_search:')
print(web_search_spec['description'])
