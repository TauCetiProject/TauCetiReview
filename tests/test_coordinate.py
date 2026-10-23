#!/usr/bin/env python3
"""Git-only contention, outage progress and cleanup without coordination comment traffic."""
import contextlib
import io
import pathlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import cli

H = "a" * 40


class Lease:
    def __init__(self):
        self.started = self.released = 0

    def start_heartbeat(self):
        self.started += 1

    def release(self):
        self.released += 1


@contextlib.contextmanager
def coordination(rc):
    lease = Lease()
    with patch.object(cli, 'git_claim_for', return_value=(lease, rc)) as acquire, \
         patch.object(cli, 'run', side_effect=AssertionError('coordination must not call gh')), \
         patch.object(cli, 'issue_comments', side_effect=AssertionError('old comments must be ignored')), \
         patch.object(cli, '_install_claim_cleanup') as cleanup, \
         patch.object(cli, 'record_coordination') as receipt, \
         patch.object(cli, '_ACTIVE_GIT_CLAIMS', []):
        yield lease, acquire, cleanup, receipt


def test_git_owner_renews_and_releases_without_comment_traffic():
    with coordination(0) as (lease, acquire, cleanup, receipt):
        assert cli.coordinate('o/r', 1, H, ['claude', 'codex']) == ['claude', 'codex']
        assert acquire.call_args.args[:3] == ('o/r', 1, H)
        assert lease.started == 1
        cleanup.assert_called_once()
        assert receipt.call_args.kwargs == {'git_held': True}
        cli._release_active_claims()
        cli._release_active_claims()
        assert lease.released == 1


def test_busy_git_claim_yields_before_inference_without_comment_traffic():
    with coordination(1) as (lease, acquire, cleanup, receipt):
        assert cli.coordinate('o/r', 1, H, ['claude']) == []
        assert lease.started == lease.released == 0
        cleanup.assert_not_called()
        receipt.assert_not_called()


def test_git_outage_proceeds_without_comment_fallback():
    with coordination(2) as (lease, acquire, cleanup, receipt):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            assert cli.coordinate('o/r', 1, H, ['claude']) == ['claude']
        assert 'concurrent duplicate review is possible' in stderr.getvalue()
        assert lease.started == 0
        cleanup.assert_not_called()
        assert receipt.call_args.kwargs == {'git_held': False}


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_'):
            fn()
            print(f'ok {name}')
