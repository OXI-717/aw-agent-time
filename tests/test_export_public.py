import os
from pathlib import Path
import shutil
import subprocess

import pytest

from export_public import EMAIL, IDENTITY, export, public_files

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/fast_tick.sh').write_text('#!/bin/sh\nexit 0\n')
    (root / 'scripts/fast_tick.sh').chmod(0o755)
    (root / 'README.md').write_text('Synthetic public fixture\n')
    (root / 'public-files.txt').write_text('README.md\npublic-files.txt\nscripts/fast_tick.sh\n')
    if (ROOT / '.gh-account').is_file():
        shutil.copyfile(ROOT / '.gh-account', root / '.gh-account')
    return root


def git(dest, *args):
    return subprocess.check_output(['git', '-C', str(dest), *args], text=True).strip()


def test_export_exact_files_fresh_history_identity_and_executable(source, tmp_path):
    (source / 'scripts/private_probe.py').write_text('synthetic_secret = True\n')
    (source / 'scripts/nested').mkdir()
    (source / 'scripts/nested/private.py').write_text('synthetic_secret = True\n')
    (source / 'scripts/nested/private.json').write_text('{"synthetic_secret":true}\n')
    (source / 'config.toml').write_text('private = true\n')
    dest = tmp_path / 'public'
    export(source, dest)
    assert git(dest, 'ls-files').splitlines() == ['README.md', 'public-files.txt', 'scripts/fast_tick.sh']
    assert git(dest, 'rev-list', '--count', '--all') == '1'
    assert git(dest, 'log', '-1', '--format=%an <%ae>|%cn <%ce>') == f'{IDENTITY} <{EMAIL}>|{IDENTITY} <{EMAIL}>'
    assert git(dest, 'remote') == ''
    assert not (dest / '.gh-account').exists()
    assert os.access(dest / 'scripts/fast_tick.sh', os.X_OK)
    assert git(dest, 'ls-files', '--stage', 'scripts/fast_tick.sh').startswith('100755 ')
    assert not (dest / 'scripts/private_probe.py').exists()
    assert not (dest / 'scripts/nested').exists()


def test_export_refuses_existing_nonempty_destination(source, tmp_path):
    dest = tmp_path / 'public'
    dest.mkdir()
    (dest / 'keep.txt').write_text('unchanged')
    with pytest.raises(ValueError, match='destination must not exist'):
        export(source, dest)
    assert (dest / 'keep.txt').read_text() == 'unchanged'


def test_export_refuses_symlink_destination(source, tmp_path):
    dest = tmp_path / 'link'
    dest.symlink_to(tmp_path / 'absent', target_is_directory=True)
    with pytest.raises(ValueError, match='destination must not exist'):
        export(source, dest)
    assert dest.is_symlink()


@pytest.mark.parametrize('ancestor', [False, True])
def test_export_refuses_symlink_source_or_ancestor(source, tmp_path, ancestor):
    target = source / ('scripts' if ancestor else 'README.md')
    real = tmp_path / 'real'
    target.rename(real)
    target.symlink_to(real, target_is_directory=ancestor)
    dest = tmp_path / 'public'
    with pytest.raises(ValueError, match='symlink'):
        export(source, dest)
    assert not dest.exists()


def test_export_refuses_binary_contents(source, tmp_path):
    (source / 'README.md').write_bytes(b'text\x00binary')
    with pytest.raises(ValueError, match='binary'):
        export(source, tmp_path / 'public')
    assert not (tmp_path / 'public').exists()


def test_manifest_lists_public_release_dependencies():
    names = set((ROOT / 'public-files.txt').read_text().splitlines())
    assert {'LICENSE', 'requirements-dev.txt', 'README.ru.md', 'docs/example-report.md', 'scripts/render_launchd.py', 'scripts/tracker_config.py'} <= names
    assert 'categories.json' not in names
    assert not any(name.startswith('docs/superpowers/') for name in names)
