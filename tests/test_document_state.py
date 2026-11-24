import json
import os
from pathlib import Path
import shutil
import time

import pikepdf as q
import pytest

from vector_pdf.document import (
    Document, INVALID_AGE, JOURNAL, LIVE, LocalEditor, cleanup_temp_files,
    cleanup_workspaces, scan_recovery,
)
from vector_pdf.object_index import index_page
from vector_pdf.version import VERSION
from vector_pdf.writer import Edit
from fixtures import drawing


def text_of(path):
    return '\n'.join(run.text for run in index_page(path, 0).runs)


def test_snapshot_survives_source_change_and_deletion(tmp_path):
    source = drawing(tmp_path / 'source.pdf')
    original = source.read_bytes()
    document = Document(source)
    LocalEditor.index(document, 0)
    basis = document.basis
    assert basis.is_file()
    assert basis.read_bytes() == original
    assert not document.source_changed()
    assert document.source_stamp is not None
    assert document.snapshot_stamp is not None

    replacement = drawing(tmp_path / 'replacement.pdf')
    source.write_bytes(replacement.read_bytes())
    changed = source.read_bytes()
    assert document.source_changed()

    first = tmp_path / 'first.pdf'
    edit = Edit(0, -101, 'SNAPSHOT EDIT', 100, 120, 18, 0, family='Sans')
    LocalEditor.save(document, first, {(0, -101): edit})
    assert first.is_file()
    assert 'SNAPSHOT EDIT' in text_of(first)
    assert source.read_bytes() == changed  # the original is never rewritten

    source.unlink()
    second = tmp_path / 'second.pdf'
    LocalEditor.save(document, second, {(0, -102): Edit(0, -102, 'AFTER DELETE', 100, 150, 18, 0, family='Sans')})
    assert 'AFTER DELETE' in text_of(second)
    assert not source.exists()


def test_destination_cannot_be_the_source(tmp_path):
    source = drawing(tmp_path / 'source.pdf')
    document = Document(source)
    original = source.read_bytes()
    with pytest.raises(ValueError, match='separate'):
        LocalEditor.save(document, source, {})
    assert source.read_bytes() == original
    # Resolved aliases and hard links are also the source.
    alias = tmp_path / 'alias.pdf'
    try:
        os.link(source, alias)
    except OSError:
        alias = None
    if alias is not None:
        with pytest.raises(ValueError, match='separate'):
            LocalEditor.save(document, alias, {})
    document.cleanup()


def test_unchanged_copy_including_signed_pdf(tmp_path):
    source = drawing(tmp_path / 'source.pdf')
    document = Document(source)
    target = tmp_path / 'copy.pdf'
    LocalEditor.save(document, target, {})
    assert target.read_bytes() == source.read_bytes()

    signed = tmp_path / 'signed.pdf'
    with q.Pdf.open(source) as pdf:
        pdf.Root.Perms = q.Dictionary()
        pdf.save(signed)
    signed_document = Document(signed)
    signed_copy = tmp_path / 'signed-copy.pdf'
    LocalEditor.save(signed_document, signed_copy, {})
    assert signed_copy.read_bytes() == signed.read_bytes()

    encrypted = tmp_path / 'encrypted.pdf'
    with q.Pdf.open(source) as pdf:
        pdf.save(encrypted, encryption=q.Encryption(user='', owner='owner-password'))
    encrypted_copy = tmp_path / 'encrypted-copy.pdf'
    LocalEditor.save(Document(encrypted), encrypted_copy, {})
    assert encrypted_copy.read_bytes() == encrypted.read_bytes()
    document.cleanup()
    signed_document.cleanup()


def test_journal_recovery_scan_and_clean_close(tmp_path):
    source = drawing(tmp_path / 'source.pdf')
    document = Document(source)
    basis = document.ensure_basis()
    key, version = document.new_version()
    assert key.startswith(document.original_key + ':')
    shutil.copyfile(basis, version)
    document.write_journal(version)

    journal = Path(document.workspace.name) / JOURNAL
    payload = json.loads(journal.read_text(encoding='utf-8'))
    assert Path(payload['source']) == source
    assert Path(payload['version']) == version
    assert payload['serial'] == document.serial == 1
    assert payload['version_string'] == VERSION
    assert payload['source_stamp'] == list(document.source_stamp)
    assert isinstance(payload['timestamp'], float)

    # A live workspace must not be offered while this process owns it...
    root = Path(document.workspace.name).parent
    assert all(item.workspace != Path(document.workspace.name) for item in scan_recovery(root))
    # ...but after a crash its PID is no longer alive and it is recoverable.
    (Path(document.workspace.name) / LIVE).write_text(json.dumps({'pid': 999_999_999}), encoding='ascii')
    candidates = [item for item in scan_recovery(root) if item.workspace == Path(document.workspace.name)]
    assert len(candidates) == 1
    assert candidates[0].version == version
    assert candidates[0].source == source

    document.cleanup()
    assert not Path(document.workspace.name).exists()
    assert not journal.exists()


def test_recovery_journal_is_written_by_new_versions(tmp_path):
    source = drawing(tmp_path / 'source.pdf')
    document = Document(source)
    _, version = document.new_version()
    LocalEditor.save(document, version, {(0, -3): Edit(0, -3, 'JOURNALED', 80, 80, 14, 0, family='Sans')})
    assert (Path(document.workspace.name) / JOURNAL).is_file()
    assert version.is_file()
    document.cleanup()


def test_stale_workspace_and_scratch_cleanup(tmp_path):
    old = time.time() - 8*24*60*60
    stale = tmp_path / 'vector-pdf-stale'
    stale.mkdir()
    (stale / 'junk.txt').write_text('x')
    os.utime(stale, (old, old))

    invalid = tmp_path / 'vector-pdf-invalid'
    invalid.mkdir()
    (invalid / LIVE).write_text(json.dumps({'pid': 999_999_999}), encoding='ascii')
    os.utime(invalid, (time.time() - INVALID_AGE - 60,)*2)

    live = tmp_path / 'vector-pdf-live'
    live.mkdir()
    (live / LIVE).write_text(json.dumps({'pid': os.getpid()}), encoding='ascii')
    os.utime(live, (old, old))

    removed = cleanup_workspaces(tmp_path)
    assert removed == 2
    assert not stale.exists()
    assert not invalid.exists()
    assert live.exists()  # a live workspace is never removed, even when old

    scratch = tmp_path / '.vector-abandoned.pdf'
    scratch.write_text('x')
    os.utime(scratch, (old, old))
    user_file = tmp_path / 'drawing.pdf'
    user_file.write_text('user')
    os.utime(user_file, (old, old))
    assert cleanup_temp_files(tmp_path) == 1
    assert not scratch.exists()
    assert user_file.exists()


def test_scan_recovery_ignores_invalid_journals(tmp_path):
    bad = tmp_path / 'vector-pdf-bad'
    bad.mkdir()
    (bad / JOURNAL).write_text('{not json', encoding='utf-8')
    (tmp_path / 'unrelated').mkdir()
    assert scan_recovery(tmp_path) == []
