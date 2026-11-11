"""Durable rendered review output and one publication/recovery path for every source."""
import contextlib
import fcntl
import json
import pathlib
import shutil
import sys
import tempfile

try:
    from . import post
    from .ledger import atomic_write_json
except ImportError:
    import post
    from ledger import atomic_write_json

from tauceti_tools.forge import ForgeError
from tauceti_tools.review_source import source_for

PUBLICATION_WAIT_EXIT = 85


class PublicationPending(RuntimeError):
    pass


@contextlib.contextmanager
def workspace_lock(work, name):
    """Keep recovery and a live producer from modifying the same workspace."""
    with (pathlib.Path(work) / name).open('a') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def source_record(source):
    identity = getattr(source, 'identity', None)
    return identity.record() if identity else dict(forge=dict(kind=source.repository.forge.kind,
                                                             url=source.repository.forge.url),
                                                   name=source.repository.name)


def cleanup_inputs(work):
    for name in ('code', 'mathlib', 'dependencies', 'roadmap', 'review', 'rubric-source'):
        shutil.rmtree(pathlib.Path(work) / name, ignore_errors=True)


def retain(repo, number, plan_path, ledger_path, *, actor=None, archive_dir=None):
    plan_path, ledger_path = pathlib.Path(plan_path), pathlib.Path(ledger_path)
    plan = json.loads(plan_path.read_text())
    source = source_for(repo)
    actor = actor or post.transport_for(repo, int(number), plan['head_sha'], plan, post).identity()
    threads = [dict(action, body=pathlib.Path(action['body']).read_text())
               for action in plan.get('threads', [])]
    value = dict(version=2, source=source_record(source), repo=source.key, pr=int(number),
                 head=plan['head_sha'], actor=actor, state='prepared', ledger=str(ledger_path),
                 scoreboard=pathlib.Path(plan['scoreboard_body']).read_text(), threads=threads,
                 round=plan.get('round'), in_progress=bool(plan.get('in_progress')),
                 archive_dir=str(ledger_path.parent / 'outbox') if archive_dir is None else str(archive_dir),
                 review_error=bool(plan.get('review_error')))
    path = plan_path.with_name('publication.json')
    atomic_write_json(path, value)
    cleanup_inputs(path.parent)
    return path


def publish(repo, path, ledger_path):
    path, ledger_path = pathlib.Path(path), pathlib.Path(ledger_path)
    value = json.loads(path.read_text())
    try:
        source = source_for(repo)
    except ForgeError as error:
        print(f'review publication retained for retry ({type(error).__name__})', file=sys.stderr)
        return PUBLICATION_WAIT_EXIT
    if value['source'] != source_record(source) or pathlib.Path(value['ledger']).resolve() != ledger_path.resolve():
        raise ValueError('retained publication belongs to another source/store')
    if value['state'] == 'published':
        return 0
    if value['state'] == 'superseded':
        return 82
    # The version-one native receipt already contains rendered paid output. Keep
    # its source binding, original native actor, round and thread IDs intact.
    actor = value.get('actor')
    if actor is None and 'actor_id' in value:
        actor = source.repository.forge.key + '/users/' + str(value['actor_id'])
    try:
        with tempfile.TemporaryDirectory(prefix='publication-', dir=path.parent) as directory:
            rendered = pathlib.Path(directory)
            scoreboard = rendered / 'scoreboard.md'
            scoreboard.write_text(value['scoreboard'])
            threads = []
            for index, action in enumerate(value['threads']):
                body = rendered / f'thread-{index}.md'
                body.write_text(action['body'])
                threads.append(dict(action, body=str(body)))
            # Anchor lookup uses the retained canonical diff, not a source clone.
            diff = path.parent / 'diff.txt'
            if diff.is_file():
                shutil.copyfile(diff, rendered / 'diff.txt')
            plan = dict(head_sha=value['head'], scoreboard_body=str(scoreboard), threads=threads,
                        round=value.get('round'), in_progress=value.get('in_progress', False))
            transport = post.transport_for(source.key, value['pr'], value['head'], plan, post)
            if transport.identity() != actor:
                raise ValueError('retained review requires its original publisher')
            status = post.execute_post(source.key, value['pr'], plan, ledger_path,
                                       value.get('archive_dir', str(ledger_path.parent / 'outbox')), transport=transport)
        if status == 0:
            value['state'] = 'published'
        elif status == 82:
            value['state'] = 'superseded'
        else:
            return PUBLICATION_WAIT_EXIT
        atomic_write_json(path, value)
        cleanup_inputs(path.parent)
        return status
    except ForgeError as error:
        print(f'review publication retained for retry ({type(error).__name__})', file=sys.stderr)
        return PUBLICATION_WAIT_EXIT


def recover(repo, root, ledger_path, numbers=(), *, work_names=()):
    """No inference/provider admission: reconcile existing rendered output only."""
    root, ledger_path = pathlib.Path(root), pathlib.Path(ledger_path)
    if not root.is_dir():
        return False
    source = source_for(repo)
    pending = False
    for work in sorted(root.iterdir()):
        if work.is_symlink() or not work.is_dir() or work_names and work.name not in work_names:
            continue
        try:
            with workspace_lock(work, 'live.lock'), workspace_lock(work, 'runner.lock'):
                result = recover_workspace(repo, work, ledger_path, source, numbers,
                                           legacy_owner_checked=bool(work_names))
        except BlockingIOError:
            continue
        if result is True:
            return True
        pending |= result == PUBLICATION_WAIT_EXIT
    if pending:
        raise PublicationPending('rendered review output remains retained for publication retry')
    return False


def recover_workspace(repo, work, ledger_path, source, numbers, *, legacy_owner_checked=False):
    context = work / 'review-context.json'
    if context.is_file():
        record = json.loads(context.read_text())
        # Older workers use process ownership rather than a runner lease.
        # Their host must positively establish that ownership has ended.
        if record.get('process') and record.get('lifecycle') != 2 and not legacy_owner_checked:
            return False
    receipt = work / 'publication.json'
    plan = next((work / name for name in ('post_plan.json', 'plan.json') if (work / name).is_file()), None)
    if not receipt.is_file() and plan:
        context = work / 'review-context.json'
        if not context.is_file():
            return False
        record = json.loads(context.read_text())
        if record.get('source') != source_record(source) or record.get('store') != str(ledger_path):
            raise ValueError('retained review context belongs to another source/store')
        if numbers and record['pr'] not in numbers:
            return False
        if json.loads(plan.read_text()).get('review_changed') is False:
            cleanup_inputs(work)
            atomic_write_json(context, dict(record, complete='unchanged'))
            return False
        if json.loads(plan.read_text()).get('head_sha') != record['head']:
            raise ValueError('retained review plan has another head')
        actor = record.get('publisher')
        if actor is None and record.get('actor_id'):
            actor = source.repository.forge.key + '/users/' + str(record['actor_id'])
        receipt = retain(repo, record['pr'], plan, ledger_path, actor=actor, archive_dir=record.get('archive_dir'))
    if receipt.is_file():
        value = json.loads(receipt.read_text())
        if value['state'] != 'prepared' or numbers and value['pr'] not in numbers:
            return False
        result = publish(repo, receipt, ledger_path)
        if result == 0:
            return True
        if result == PUBLICATION_WAIT_EXIT:
            return PUBLICATION_WAIT_EXIT
    return False

