import sys

import pytest

from backend.tasks.capabilities import LocalCapabilities, CapabilityError


@pytest.fixture
def tools(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    return LocalCapabilities(tmp_path / 'state'), {'id': 'synthetic', 'workspace': str(work)}, work


@pytest.mark.parametrize('args', [{'path': []}, {'path': 'out.txt', 'content': [1]},
    {'path': 'out.txt', 'content': {'text': 'wrong'}}, {'path': 'out.txt', 'content': 'text', 'offset': []},
    {'path': 'out.txt', 'content': 'text', 'limit': True}, {'argv': [None]}, {'timeout': float('inf')}])
def test_invalid_tool_arguments_do_not_create_files(tools, args):
    capability, run, work = tools
    with pytest.raises(CapabilityError):
        capability.file_tool(run, 'write_file', args)
    assert not list(work.iterdir())


@pytest.mark.asyncio
async def test_large_unicode_command_output_has_no_split_codepoint_corruption(tools):
    capability, run, _ = tools
    events = []
    code = "import sys;sys.stdout.buffer.write(b'x'*4095+bytes.fromhex('e6b189f09f9982')*15000)"
    try:
        result = await capability.command(run, {'argv': [sys.executable, '-c', code]}, 'unicode',
            lambda kind, payload: events.append(payload['text']))
        text = ''.join(events)
        assert '\ufffd' not in text
        assert text.startswith('x' * 4095) and text.endswith('\U0001f642')
        assert len(result['output']) <= 32000
        log = (capability.data_root / 'command-logs/unicode.log').read_bytes()
        assert len(log) == 4095 + 7 * 15000 and log.decode('utf-8') == text
    finally:
        await capability.close()


@pytest.mark.parametrize('fault', ['callback', 'log-directory'])
@pytest.mark.asyncio
async def test_command_io_failure_reaps_the_real_process(tools, monkeypatch, fault):
    capability, run, _ = tools
    killed = []
    original = capability.kill

    async def track(process):
        await original(process)
        killed.append(process.returncode)

    monkeypatch.setattr(capability, 'kill', track)
    if fault == 'log-directory':
        capability.data_root.mkdir(parents=True, exist_ok=True)
        (capability.data_root / 'command-logs').write_text('blocked')

    def emit(*_):
        raise RuntimeError('synthetic callback failed')

    try:
        with pytest.raises((RuntimeError, OSError)):
            await capability.command(run, {'argv': [sys.executable, '-u', '-c',
                "import time;print('ready',flush=True);time.sleep(30)"]}, 'failure', emit)
        assert len(killed) == 1 and killed[0] is not None
        assert not capability.processes
    finally:
        await capability.close()
