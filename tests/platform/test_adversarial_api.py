"""Invalid external data must be rejected before persistence or scheduling."""
import json

from conftest import fixture_run


def test_invalid_nested_payloads_are_client_errors(world):
    coordinator, client, actors = world
    run, _ = fixture_run(coordinator, actors[0])
    rid = run['id']
    cid = client.get('/api/bootstrap').json()['snapshot']['conversations'][0]['id']
    settings_before = client.get('/api/workspace/load').json()['workspace']['settings']
    run_count = len(coordinator.store.list(all_rows=True))
    cases = [
        ('/api/messages/send', {'mode': ['task'], 'content': 'synthetic'}),
        ('/api/messages/send', {'mode': {'kind': 'chat'}, 'content': 'synthetic'}),
        ('/api/messages/send', {'mode': 'chat', 'conversationId': cid, 'content': ['not text']}),
        ('/api/messages/send', {'mode': 'chat', 'conversationId': cid, 'collaborative': 'false', 'content': 'synthetic'}),
        ('/api/runs/' + rid + '/control', {'action': ['cancel']}),
        ('/api/runs/' + rid + '/control', {'action': 'steer', 'text': ['not text']}),
        ('/api/runs/' + rid + '/control', {'action': 'resume', 'acknowledgeEffects': 'false'}),
        ('/api/runs/missing/approvals/missing', {'decision': 'allow_once'}),
        ('/api/skills/proposals/missing/review', {'reviewer': ['secretary'], 'decision': 'approve'}),
        ('/api/personas/compose', {'names': 7}),
        ('/api/personas/compose', {'names': ['unconfigured'], 'participantCount': [2]}),
        ('/api/conversations/open', {'conversationId': 'missing'}),
        ('/api/posts/publish', {'body': 'synthetic', 'audience': ['port']}),
        ('/api/posts/publish', {'body': 'synthetic', 'audience': {'kind': ['port']}}),
        ('/api/posts/publish', {'body': ['not text']}),
        ('/api/agents/persona', {'agentId': actors[0], 'systemPrompt': ['not text']}),
    ]
    for name in ('maxConnectedAgents', 'socialIntervalMinutes'):
        for value in ([1], {'wrong': 1}, 'NaN', -1, 10**100):
            cases.append(('/api/workspace/save', {'workspace': {'settings': {name: value}}}))
    for name in ('connectedAgentIds', 'chatTimeoutSeconds', 'runTimeoutSeconds'):
        cases.append(('/api/workspace/save', {'workspace': {'settings': {name: {'wrong': 1}}}}))
    cases.extend([
        ('/api/messages/send', {'conversationId': cid, 'content': 'x' * 30001, 'mode': 'chat'}),
        ('/api/messages/send', {'conversationId': cid, 'content': 'synthetic', 'mode': 'chat', 'attachments': ['a'] * 5}),
        ('/api/profile', {'name': ' ' * 3}),
        ('/api/profile', {'name': 'x' * 41}),
        ('/api/profile', {'name': 'Synthetic', 'avatar': 'data:image/png;base64,invalid'}),
        ('/api/life', {'hourlyCalls': -1}),
        ('/api/life', {'hourlyCalls': 1001}),
        ('/api/life', {'quietStart': 24}),
        ('/api/life', {'paused': [False]}),
        ('/api/actors/' + actors[0] + '/goals', {'title': ' ', 'motivation': 'test', 'nextStep': 'test'}),
    ])
    problems = []
    client._transport.raise_server_exceptions = False
    try:
        for path, payload in cases:
            response = client.post(path, json=payload)
            if not 400 <= response.status_code < 500:
                problems.append({'path': path, 'payload': payload, 'status': response.status_code,
                                 'response': response.text[:120]})
    finally:
        client._transport.raise_server_exceptions = True
    assert not problems, json.dumps(problems, ensure_ascii=False, indent=2)
    assert client.get('/api/workspace/load').json()['workspace']['settings'] == settings_before
    assert len(coordinator.store.list(all_rows=True)) == run_count
    assert client.get('/api/posts').json()['posts'] == []


def test_explicit_disconnect_and_timeout_settings_are_persisted(world, tmp_path):
    coordinator, client, actors = world
    for value in [actors[:2], []]:
        response = client.post('/api/workspace/save', json={'workspace': {'settings': {
            'connectedAgentIds': value, 'chatTimeoutSeconds': '15', 'runTimeoutSeconds': '180',
            'maxTaskMembers': '2', 'authorizedWorkspaceRoot': ''}}})
        assert response.status_code == 200, response.text
        settings = coordinator.settings_loader()
        assert settings['connectedAgentIds'] == value
        assert settings['chatTimeoutSeconds'] == '15' and settings['runTimeoutSeconds'] == '180'
        assert settings['maxTaskMembers'] == '2' and settings['authorizedWorkspaceRoot'] == ''


def test_validation_errors_do_not_echo_secrets_or_write_partial_settings(world):
    _, client, _ = world
    before = client.get('/api/workspace/load').json()['workspace']['settings']
    response = client.post('/api/workspace/save', json={'workspace': {'settings': {
        'llmApiKey': 'synthetic-secret', 'llmModel': 'should-not-be-saved', 'maxConnectedAgents': -1}}})
    assert response.status_code == 422
    assert 'synthetic-secret' not in response.text
    assert client.get('/api/workspace/load').json()['workspace']['settings'] == before


def test_missing_resources_and_bad_query_types_never_raise_server_errors(world):
    _, client, _ = world
    paths = ['/api/runs/missing', '/api/runs/missing/artifacts/0', '/api/actors/missing/methods',
        '/api/actors/missing/mind', '/api/actors/missing/goals', '/api/runs/missing/transcripts',
        '/api/runtime/events?after=not-a-number', '/api/posts?limit=not-a-number',
        '/api/posts?mode=invalid', '/api/actors/missing/life', '/api/runs/missing/calls/missing/log']
    client._transport.raise_server_exceptions = False
    try:
        for path in paths:
            response = client.get(path)
            assert response.status_code < 500, (path, response.text)
    finally:
        client._transport.raise_server_exceptions = True


def test_corrupt_and_oversized_attachments_are_rejected_without_files(world, tmp_path):
    _, client, _ = world
    work = tmp_path / 'synthetic-files'
    work.mkdir()
    client.post('/api/workspace/save', json={'workspace': {'settings': {'authorizedWorkspaceRoot': str(work)}}}).raise_for_status()
    cid = client.get('/api/bootstrap').json()['snapshot']['conversations'][0]['id']
    path = '/api/attachments'
    for name, body, status in [('empty.txt', b'', 400), ('bad.png', b'not an image', 400),
                              ('huge.txt', b'x' * (8 * 1024 * 1024 + 1), 413)]:
        response = client.post(path, params={'conversationId': cid, 'name': name}, content=body)
        assert response.status_code == status, response.text
    assert not list(work.iterdir())


def test_corrupt_attachment_metadata_and_missing_file_return_not_found(world, tmp_path):
    _, client, _ = world
    work = tmp_path / 'synthetic-files'
    work.mkdir()
    client.post('/api/workspace/save', json={'workspace': {'settings': {'authorizedWorkspaceRoot': str(work)}}}).raise_for_status()
    cid = client.get('/api/bootstrap').json()['snapshot']['conversations'][0]['id']
    uploaded = client.post('/api/attachments', params={'conversationId': cid, 'name': 'source.txt'}, content=b'synthetic').json()
    metadata = work / '.juus-uploads' / (uploaded['id'] + '.json')
    original = metadata.read_text(encoding='utf-8')
    for content in ['[]', 'null', '{broken', '{"mime": []}',
                    json.dumps({**json.loads(original), 'mime': []}),
                    json.dumps({**json.loads(original), 'suffix': []})]:
        metadata.write_text(content, encoding='utf-8')
        response = client.get('/api/attachments/' + uploaded['id'], params={'conversationId': cid})
        assert response.status_code == 404, response.text
    metadata.write_text(original, encoding='utf-8')
    (work / '.juus-uploads' / (uploaded['id'] + '.txt')).unlink()
    assert client.get('/api/attachments/' + uploaded['id'], params={'conversationId': cid}).status_code == 404


def test_gateway_invalid_names_and_arguments_are_rejected_without_side_effects(world, tmp_path):
    coordinator, client, actors = world
    work = tmp_path / 'synthetic-tools'
    work.mkdir()
    run, _ = fixture_run(coordinator, actors[0])
    coordinator.store.update(run['id'], workspace=str(work), mode='task', status='running')
    coordinator.tokens['synthetic-token'] = (run['id'], actors[0], 'main')
    headers = {'Authorization': 'Bearer synthetic-token'}
    for index, name in enumerate([[], {}, None, 7]):
        response = client.post('/api/internal/tool', headers=headers,
            json={'name': name, 'args': {}, 'callId': 'badname' + str(index)})
        assert response.status_code in {400, 403, 422}, response.text
    for index, args in enumerate([{'path': 'out.txt', 'content': [1]}, {'path': [], 'content': 'text'}]):
        response = client.post('/api/internal/tool', headers=headers,
            json={'name': 'write_file', 'args': args, 'callId': 'badargs' + str(index)})
        assert response.status_code == 200 and response.json()['status'] == 'failed'
        assert coordinator.store.call('badargs' + str(index))['status'] == 'failed'
    assert not list(work.iterdir())
