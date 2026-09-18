import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tracker_config


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match='configuration'):
        tracker_config.load_config(tmp_path / 'missing.toml')


def test_toml_literals_and_relative_environment_paths(monkeypatch, tmp_path):
    cfg = tmp_path / 'settings.toml'
    cfg.write_text('[paths]\nstate_dir = "state#1"\n[tracker]\nclient_name = "client#literal"\n')
    monkeypatch.setenv('AW_TRACKER_CATEGORIES_FILE', 'cats.json')
    parsed = tracker_config.load_config(cfg)
    assert parsed.client_name == 'client#literal'
    assert parsed.state_dir == tmp_path / 'state#1'
    assert parsed.categories_file == tmp_path / 'cats.json'
    assert parsed.index_db == tmp_path / 'state#1/jsonl_index.sqlite'


def test_runtime_consumers_share_config(tmp_path):
    cfg = tmp_path / 'settings.toml'
    cfg.write_text('''[tracker]
bucket_prefix = "test-track"
event_type_prefix = "test.events"
client_name = "test-client"
aw_host = "http://127.0.0.1:15600/"
[paths]
state_dir = "state"
index_db = "db/index.sqlite"
task_runner_cache = "cache/projects.json"
claude_projects_dir = "logs/claude"
codex_sessions_dir = "logs/codex"
task_runner_global_dir = "runner"
layer25_project_roots = ["projects"]
excluded_scan_roots = ["projects/excluded"]
obsidian_folder = "reports"
''')
    code = '''import json,lib_aw,tracker_layer1 as a,tracker_layer2 as b,tracker_layer25 as c,jsonl_indexer as d,lint_categories as e,aggregate as f,aw_watcher_tmux_pane as g
print(json.dumps(dict(db=str(d.DB_PATH),layerdb=str(a.INDEX_DB),cache=str(c.CACHE_FILE),claude=str(d.CLAUDE_DIR),codex=str(b.CODEX_DIR),global_dir=str(c.GLOBAL_TR),types=[a.OUTPUT_EVENT_TYPE,b.OUTPUT_EVENT_TYPE,c.OUTPUT_EVENT_TYPE],host=e.AW_HOST,watcher_host=g.AW_HOST,reports=str(f.OBSIDIAN_FOLDER),excluded=sorted(f.FS_NO_WALK_PREFIXES))))'''
    env = dict(os.environ, AW_TRACKER_CONFIG=str(cfg), PYTHONPATH=str(Path(tracker_config.__file__).parent))
    for key in ('AW_TRACKER_BUCKET_PREFIX', 'AW_TRACKER_CLIENT_NAME', 'AW_TRACKER_CATEGORIES_FILE'):
        env.pop(key, None)
    result = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    assert actual == dict(db=str(tmp_path/'db/index.sqlite'),layerdb=str(tmp_path/'db/index.sqlite'),cache=str(tmp_path/'cache/projects.json'),claude=str(tmp_path/'logs/claude'),codex=str(tmp_path/'logs/codex'),global_dir=str(tmp_path/'runner'),types=['test.events.state','test.events.engagement','test.events.autonomous'],host='http://127.0.0.1:15600',watcher_host='http://127.0.0.1:15600',reports=str(tmp_path/'reports'),excluded=[str(tmp_path/'projects/excluded')])


def test_missing_environment_config_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv('AW_TRACKER_CONFIG', str(tmp_path / 'missing.toml'))
    with pytest.raises(FileNotFoundError, match='configuration'):
        tracker_config.load_config()


def test_discovery_prunes_configured_roots(monkeypatch, tmp_path):
    import dataclasses
    import tracker_layer25 as layer
    allowed = tmp_path / 'project-a/.task-runner/status.json'
    excluded = tmp_path / 'excluded/project-b/.task-runner/status.json'
    for path in (allowed, excluded):
        path.parent.mkdir(parents=True)
        path.write_text('{}')
    monkeypatch.setattr(layer, 'TRACKER_CONFIG', dataclasses.replace(layer.TRACKER_CONFIG, layer25_project_roots=[tmp_path], excluded_scan_roots=[tmp_path/'excluded']))
    assert layer._discover_status_files_fs() == [str(allowed)]


def test_linter_coverage_uses_configured_endpoint(monkeypatch):
    import io
    import lint_categories as lint
    urls = []
    monkeypatch.setattr(lint, 'AW_HOST', 'http://127.0.0.1:15600')
    monkeypatch.setattr(lint, 'bucket_name', lambda suffix: 'custom-' + suffix)
    monkeypatch.setattr(lint.urllib.request, 'urlopen', lambda url, **kw: urls.append(url) or io.StringIO('[]'))
    lint.check_coverage([], 1)
    assert urls[0].startswith('http://127.0.0.1:15600/api/0/buckets/custom-states/events?')


def test_tmux_detection_uses_configured_claude_root(monkeypatch, tmp_path):
    import dataclasses
    import aw_watcher_tmux_pane as watcher
    root = tmp_path / 'alternate-conversations'
    log = root / '-Users-example-project-a/session.jsonl'
    monkeypatch.setattr(watcher, 'CONFIG', dataclasses.replace(watcher.CONFIG, claude_projects_dir=root))
    monkeypatch.setattr(watcher, '_children_pids', lambda pid: [(100, 'claude')])
    monkeypatch.setattr(watcher.subprocess, 'check_output', lambda *a, **kw: ('n'+str(log)+'\n').encode())
    monkeypatch.setattr(watcher, '_decode_claude_project_path_full', lambda value: '/Users/example/project-a')
    assert watcher._detect_ai_project_inner(10) == ('project-a', 'claude', str(log))


def test_aw_bucket_creation_uses_configured_client(monkeypatch):
    import dataclasses
    import lib_aw
    requests = []
    monkeypatch.setattr(lib_aw, 'TRACKER_CONFIG', dataclasses.replace(lib_aw.TRACKER_CONFIG, client_name='configured-client'))
    monkeypatch.setattr(lib_aw, '_request', lambda *a, **kw: requests.append((a, kw)))
    lib_aw.ensure_bucket('test-bucket', 'test.state')
    assert requests[0][1]['body']['client'] == 'configured-client'
