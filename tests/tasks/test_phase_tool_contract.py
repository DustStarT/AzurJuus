import json

from backend.tasks.capabilities import CATALOG, phase_actions
from backend.tasks.deepseek_bridge import DeepSeekHarnessBridge


def test_native_plugin_catalog_contains_only_phase_authorized_actions(tmp_path):
    for phase in ('chat', 'planner', 'reviewer', 'worker'):
        bridge = DeepSeekHarnessBridge(tmp_path / phase, {'_allowedActions': phase_actions(phase)},
                                       'http://127.0.0.1/tool', 'synthetic-token', None)
        env = bridge._environment()
        assert json.loads(env['AZURJUUS_TOOL_CATALOG']) == {name: CATALOG[name] for name in phase_actions(phase)}
        assert '/kernel/native/workspace.mjs' in bridge._patch().read_text(encoding='utf-8')
        assert 'mcp' not in bridge._patch().read_text(encoding='utf-8').lower()
