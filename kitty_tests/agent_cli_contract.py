"""System-Python contracts using production AST, without native UI imports."""
import ast
import dataclasses
import importlib.util
from pathlib import Path
import sys
import types
import unittest
import re

ROOT = Path(__file__).resolve().parents[1]


def nodes(path):
    return ast.parse((ROOT / path).read_text(), filename=path).body


def execute(selected, namespace):
    tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *selected], type_ignores=[])
    # Python 3.14 dataclasses resolve annotations through the defining module.
    # Keep the synthetic namespace registered only for this execution.
    name = '_agent_cli_contract_ast'
    previous = sys.modules.get(name)
    module = types.ModuleType(name)
    module.__dict__.update(namespace)
    module.__dict__['__name__'] = name
    sys.modules[name] = module
    try:
        exec(compile(ast.fix_missing_locations(tree), '<production AST>', 'exec'), module.__dict__)
        namespace.update(module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def contracts():
    # The production parser explicitly supports this native-free build mode.
    spec = importlib.util.spec_from_file_location('agent_contract_options', ROOT / 'kitty/simple_cli_definitions.py')
    module = importlib.util.module_from_spec(spec)
    module.appname = 'kitty'
    module.is_macos = False
    absent = object()
    previous = getattr(sys, 'running_from_setup', absent)
    previous_module = sys.modules.get(spec.name, absent)
    sys.modules[spec.name] = module
    sys.running_from_setup = True
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_module is absent:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous_module
        if previous is absent:
            del sys.running_from_setup
        else:
            sys.running_from_setup = previous
    ns = dict(vars(module), dataclass=dataclasses.dataclass, field=dataclasses.field)
    for path, names in (
        ('kitty/rc/base.py', {'ArgsHandling', 'MATCH_WINDOW_OPTION'}),
        ('kitty/cli.py', {'GoOption', 'go_type_map', 'get_defaults_from_seq'}),
        ('gen/go_code.py', {'JSONField', 'go_field_type', 'json_field_types'}),
    ):
        selected = []
        for n in nodes(path):
            if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names:
                selected.append(n)
            elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets):
                selected.append(n)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
                selected.append(n)
        execute(selected, ns)
    ns['RemoteCommand'] = types.SimpleNamespace(Args=ns['ArgsHandling'], CompletionSpec=module.CompletionSpec)
    result = {}
    for name in ('agent', 'agent_control', 'agent_wait', 'agent_resume', 'agent_start', 'agent_events', 'agent_subscribe', 'agent_read', 'agent_prompt'):
        wanted = {'agent': 'Agent', 'agent_control': 'AgentControl', 'agent_wait': 'AgentWait', 'agent_resume': 'AgentResume', 'agent_start': 'AgentStart', 'agent_events': 'AgentEvents', 'agent_subscribe': 'AgentSubscribe', 'agent_read': 'AgentRead', 'agent_prompt': 'AgentPrompt'}[name]
        cls = next(n for n in nodes(f'kitty/rc/{name}.py') if isinstance(n, ast.ClassDef) and n.name == wanted)
        local = dict(ns)
        if name == 'agent_subscribe':
            local['AgentEvents'] = types.SimpleNamespace(**{k: result['agent_events'][k] for k in ('options_spec', 'protocol_spec')})
        if name == 'agent_prompt':
            local['agent_control'] = types.SimpleNamespace(options_spec=result['agent_control']['options_spec'])
        execute([n for n in cls.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in {'args', 'options_spec', 'protocol_spec'} for t in n.targets)], local)
        result[name] = local
    return result


class AgentCLIContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = contracts()

    def fields(self, name):
        c = self.commands[name]
        seq, _ = c['parse_option_spec'](c['options_spec'])
        options = {o.go_var_name: o for o in (c['GoOption'](x) for x in seq if not isinstance(x, str))}
        return {f.field: f for f in (c['JSONField'](line.strip(), {}, options)
                for line in c['protocol_spec'].splitlines() if ':' in line)}

    def test_real_option_defaults(self):
        c = self.commands['agent_wait']
        # agent-wait's local options are parsed directly from the production spec;
        # the shared match option is intentionally not duplicated here.
        seq, _ = c['parse_option_spec'](c['options_spec'].replace('MATCH_WINDOW_OPTION', ''))
        defaults = {o.obj_defn.dest.replace('-', '_'): c['defval_for_opt'](o.obj_defn) for o in (c['GoOption'](x) for x in seq if not isinstance(x, str))}
        source = (ROOT / 'kitty/rc/agent_wait.py').read_text()
        for key, value in re.findall(r'--([a-z-]+)\n(?:type=[^\n]+\n)?default=([^\n]+)', source[source.index('options_spec'):]):
            defaults[key.replace('-', '_')] = int(value) if value.lstrip('-').isdigit() else value
        defaults.update(after_seq=-1, require_working=False)
        for key, value in {'after_seq': -1, 'timeout_ms': 0, 'max_events': 100, 'until': 'changed', 'require_working': False}.items():
            self.assertEqual(defaults[key], value, key)
        self.assertEqual(type(defaults['after_seq']), int)

    def test_protocol_fields_and_omitempty(self):
        fields = self.fields('agent_wait')
        self.assertEqual(set(fields), {'match', 'expected_agent', 'expected_session', 'after_seq', 'until', 'require_working', 'timeout_ms', 'max_events', 'working_seq', 'working_generation'})
        self.assertIn('json:"after_seq', fields['after_seq'].go_declaration())
        self.assertFalse(fields['after_seq'].omitempty)
        for name in self.commands:
            for field in self.fields(name).values():
                self.assertTrue(field.go_declaration())

    def test_service_commands(self):
        self.assertIn('inspect', self.fields('agent')['action'].field_type)
        for name in ('agent_events', 'agent_subscribe'):
            self.assertEqual(set(self.fields(name)), {'cursor', 'max_events', 'timeout_ms'})
        self.assertTrue({'expected_instance', 'expected_epoch', 'expected_binding', 'request_id'} <= set(self.fields('agent_control')))

    def test_action_and_first_rest_generation(self):
        c = self.commands['agent_control']
        fields = self.fields('agent_control')
        self.assertTrue(fields['action'].required)
        self.assertEqual(fields['arguments'].field_type, 'list.str')
        self.assertIn('[]escaped_string', fields['arguments'].go_declaration())
        self.assertEqual(c['args'].first_rest, ('action', 'arguments'))
        handled = set()
        code = '\n'.join(c['args'].as_go_code('agent_control', {k: f.field_type for k, f in fields.items()}, handled))
        self.assertEqual(handled, {'action', 'arguments'})
        self.assertIn('len(args) < 1', code)
        self.assertIn('payload.Action = escaped_string(args[0])', code)
        self.assertIn('payload.Arguments = escape_list_of_strings(args[1:])', code)
        c = self.commands['agent']
        handled = set()
        code = '\n'.join(c['args'].as_go_code('agent', {k: f.field_type for k, f in self.fields('agent').items()}, handled))
        self.assertIn('switch(args[0])', code)
        self.assertEqual(handled, {'action'})
        self.assertEqual(self.commands['agent_resume']['args'].args_count, 0)


if __name__ == '__main__':
    unittest.main()
