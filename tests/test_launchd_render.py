import plistlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_install_renders_launchd_templates_without_loading_services(tmp_path):
    out_dir = tmp_path / "launchd out"
    python = "/usr/bin/python3"
    subprocess.run(
        [
            "bash",
            str(ROOT / "install.sh"),
            "--render-launchd",
            "--include-tmux",
            "--launchd-output",
            str(out_dir),
            "--label-prefix",
            "com.example.aw-agent-time",
            "--python",
            python,
            "--repo-dir",
            str(ROOT),
        ],
        check=True,
        cwd=ROOT,
    )

    tracker = plistlib.loads((out_dir / "com.example.aw-agent-time.tracker.plist").read_bytes())
    assert tracker["Label"] == "com.example.aw-agent-time.tracker"
    assert tracker["ProgramArguments"][0] == python
    assert tracker["ProgramArguments"][2] == str(ROOT / "scripts" / "aggregate.py")
    assert tracker["StandardOutPath"] == str(ROOT / "state" / "logs" / "tracker.out.log")

    tmux = plistlib.loads((out_dir / "com.example.aw-agent-time.watcher-tmux-pane.plist").read_bytes())
    assert tmux["Label"] == "com.example.aw-agent-time.watcher-tmux-pane"
    assert tmux["ProgramArguments"][2].endswith("aw_watcher_tmux_pane.py")


def test_render_uses_config_and_preserves_special_characters(tmp_path):
    import os
    import sys
    repo = tmp_path / 'A&B <project>'
    config = tmp_path / 'local.toml'
    config.write_text(f'[launchd]\nlabel_prefix="org.example.time"\npython="{sys.executable}"\n')
    env = dict(os.environ, AW_TRACKER_CONFIG=str(config))
    out = tmp_path / 'jobs'
    result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--render-launchd',
        '--repo-dir', str(repo), '--launchd-output', str(out)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    tracker = plistlib.loads((out/'org.example.time.tracker.plist').read_bytes())
    assert tracker['ProgramArguments'] == [sys.executable, '-u', str(repo/'scripts/aggregate.py'), '--today']
    fast = plistlib.loads((out/'org.example.time.tracker-fast.plist').read_bytes())
    assert fast['ProgramArguments'] == ['/bin/bash', str(repo/'scripts/fast_tick.sh')]
    assert fast['EnvironmentVariables']['PYTHON'] == sys.executable
    assert fast['EnvironmentVariables']['AW_TRACKER_CONFIG'] == str(config)
    assert not list(out.glob('*watcher-window*'))
    assert not list(out.glob('*watcher-afk*'))


def test_render_refuses_to_overwrite_existing_job(tmp_path):
    out = tmp_path/'jobs'
    out.mkdir()
    job = out/'com.example.aw-agent-time.tracker.plist'
    job.write_text('existing user job')
    result = subprocess.run(['bash', str(ROOT/'install.sh'), '--render-launchd',
        '--launchd-output', str(out)], capture_output=True, text=True)
    assert result.returncode != 0
    assert job.read_text() == 'existing user job'
    assert not (out/'com.example.aw-agent-time.tracker-fast.plist').exists()


def test_renderer_preserves_supported_environment_overrides(tmp_path):
    import os
    config = tmp_path/'config.toml'
    config.write_text('[tracker]\nbucket_prefix="file-prefix"\n')
    env = dict(os.environ, AW_TRACKER_CONFIG=str(config),
        AW_TRACKER_BUCKET_PREFIX='env-prefix', AW_TRACKER_CLIENT_NAME='env-client',
        AW_TRACKER_CATEGORIES_FILE='private-categories.json')
    out = tmp_path/'jobs'
    result = subprocess.run(['bash', str(ROOT/'install.sh'), '--render-launchd',
        '--launchd-output', str(out)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    document = plistlib.loads((out/'com.example.aw-agent-time.tracker.plist').read_bytes())
    for key in ('AW_TRACKER_BUCKET_PREFIX', 'AW_TRACKER_CLIENT_NAME', 'AW_TRACKER_CATEGORIES_FILE'):
        assert document['EnvironmentVariables'][key] == env[key]
