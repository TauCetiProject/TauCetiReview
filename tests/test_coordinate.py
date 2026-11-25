#!/usr/bin/env python3
"""Git-only contention, outage progress and cleanup without coordination comment traffic."""
import contextlib
import io
import pathlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import cli
from tauceti_tools.claims import GitClaim

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



class RealLease(GitClaim):
    """A real GitClaim whose claim.sh verbs are recorded instead of run."""

    def __init__(self):
        super().__init__('o/claims', 'review/o/r/1/' + H, 'w-nonce', ttl=3)
        self.verbs = []

    def command(self, verb):
        self.verbs.append(verb)
        return 0


def run_main(outcome, *, inference):
    lease = RealLease()
    assert lease.acquire() == 0
    lease.start_heartbeat()

    def fake_main(cleanup, leases, phase):
        cli._ACTIVE_GIT_CLAIMS.append(lease)
        phase['inference'] = inference
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    with patch.object(cli, '_main', side_effect=fake_main), patch.object(cli, '_ACTIVE_GIT_CLAIMS', []), \
         contextlib.redirect_stderr(io.StringIO()):
        result = cli.main()
        left = list(cli._ACTIVE_GIT_CLAIMS)
    return result, lease, left


def test_retained_publication_keeps_the_head_lease_until_its_ttl():
    wait = cli.publication.PUBLICATION_WAIT_EXIT
    for outcome in (wait, cli.publication.PublicationPending('retained'), OSError('write'),
                    cli.ForgeError('AmbiguousWrite')):
        result, lease, left = run_main(outcome, inference=True)
        assert result == wait, (outcome, result)
        assert 'release' not in lease.verbs, outcome
        assert lease.thread is None and lease.stop.is_set() and not lease.held, outcome
        assert left == [], 'atexit must not release a kept lease'
        lease.release()
        assert 'release' not in lease.verbs, 'a kept lease is never released later in this process'


def test_other_outcomes_still_release_the_lease():
    for outcome, inference in ((0, True), (cli.publication.PUBLICATION_BLOCKED_EXIT, True),
                               (cli.publication.PublicationPending('before'), False)):
        result, lease, left = run_main(outcome, inference=inference)
        assert lease.verbs.count('release') == 1, (outcome, lease.verbs)
        assert left == []


def test_reporting_failure_does_not_abort_a_review():
    with patch('tauceti_tools.claims.record_review_start', side_effect=TypeError('receipt unavailable')):
        cli.record_coordination('o/r', 1, H, 'nonce', git_held=True)


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_'):
            fn()
            print(f'ok {name}')
