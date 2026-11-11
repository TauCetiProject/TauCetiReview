"""Native API operations for the shared trusted publication policy in post.py."""
import hashlib
import json
import pathlib
import re

from tauceti_tools.forge import AmbiguousWrite, Incomplete
from tauceti_tools.review_source import source_for


class PublicationTransport:
    def __init__(self, repo, number, head, plan, helpers, source=None):
        self.repo, self.number, self.head, self.plan, self.helpers = repo, number, head, plan, helpers
        self.source = source

    def body(self, path):
        return pathlib.Path(path).read_text()

    def current_pull(self):
        raise NotImplementedError

    def current(self):
        if self.source is None:
            return True  # Legacy direct helper callers supply their own snapshot.
        row = self.current_pull()
        if row is None:
            raise Incomplete('publication freshness is unavailable')
        return row['state'] == 'open' and row['head']['sha'] == self.head


class GitHubPublication(PublicationTransport):
    def current_pull(self):
        return self.helpers.gh_api('GET', f'/repos/{self.repo}/pulls/{self.number}')

    def identity(self):
        return self.helpers.current_login()

    def roots(self):
        return self.helpers.find_review_roots(self.repo, self.number)

    def editable(self, root, action=None):
        return True

    def edit_finding(self, identifier, action, failures):
        return self.helpers.gh_api('PATCH', f'/repos/{self.repo}/pulls/comments/{identifier}',
                                   body_file=action['body'], failures=failures,
                                   action=f"required thread PATCH {action['rubric']}")

    def create_finding(self, action, failures):
        return self.helpers.gh_api('POST', f'/repos/{self.repo}/pulls/{self.number}/comments',
                                   fields=dict(commit_id=self.head, path=action['path'], subject_type='file'),
                                   body_file=action['body'], failures=failures,
                                   action=f"required thread POST {action['rubric']}")

    def board_comments(self):
        return self.helpers.find_scoreboard_comments(self.repo, self.number)

    def comment(self, method, identifier, body_file=None, failures=None, action=''):
        endpoint = (f'/repos/{self.repo}/issues/{self.number}/comments' if method == 'POST'
                    else f'/repos/{self.repo}/issues/comments/{identifier}')
        return self.helpers.gh_api(method, endpoint, body_file=body_file, failures=failures, action=action)

    def replied(self, rubric, through, me):
        return self.helpers.already_replied(self.repo, self.number, rubric, through, me)

    def reply(self, parent, action, failures):
        return self.helpers.gh_api('POST', f'/repos/{self.repo}/pulls/{self.number}/comments/{parent}/replies',
                                   body_file=action['body'], failures=failures,
                                   action=f"thread reply {action['rubric']}")

    def resolve(self, identifier):
        self.helpers.resolve_thread(self.repo, self.number, identifier)

class NativePublication(PublicationTransport):
    def __init__(self, *args, source):
        super().__init__(*args)
        self.source, self.api, self.repository = source, source.api, source.repository
        self.prefix = 'repos/' + self.repository.name

    def current_pull(self):
        return self.source.pull(self.number)

    def identity(self):
        return self.source.publisher_key()

    def principal(self, row):
        return self.repository.forge.key + '/users/' + str(row.get('id'))

    def body(self, path):
        return pathlib.Path(path).read_text().replace('https://github.com/' + self.repo, self.repository.url)

    def roots(self):
        return self.helpers.normalize_roots(self.source.findings(self.number), self.principal,
                                            reviewed_head=lambda row, meta: row.get('commit_id'))

    def editable(self, root, action=None):
        return root.get('head_sha') == self.head and (action is None or
            (root.get('thread_kind') == 'issue-comment') == (self.position(action) is None))

    def edit_finding(self, identifier, action, failures):
        root = next((row for row in self.roots() if row['id'] == identifier), None)
        if root is None or root['login'] != self.identity():
            raise Incomplete('native finding ownership unavailable')
        body = self.body(action['body'])
        if root.get('thread_kind') == 'issue-comment':
            body = f"<!--tauceti-source-head:{root['head_sha']}-->\n" + body
        self.api.request('PATCH', f'{self.prefix}/issues/comments/{identifier}', {'body': body})
        exact = [row for row in self.roots() if row['id'] == identifier
                 and row['login'] == self.identity() and row['body'] == body]
        if not exact:
            raise AmbiguousWrite('native finding edit requires confirmed read-back')
        return exact[0]

    def position(self, action):
        # Native reviews need a changed-line position; use the prepared exact
        # diff. Unrepresentable filenames/anchors use a head-bound conversation
        # finding, retaining the same rubric/reply semantics.
        if action.get('conversation'):
            return None
        path = action.get('path') or ''
        diff = pathlib.Path(self.plan['scoreboard_body']).parent / 'diff.txt'
        position, selected, previous = None, False, False
        if diff.is_file():
            for line in diff.read_text(errors='surrogateescape').splitlines():
                if line.startswith('diff --git '):
                    selected = previous = False
                elif line.startswith('--- '):
                    previous = line[4:] == 'a/' + path
                elif line.startswith('+++ '):
                    selected = line[4:] == 'b/' + path
                    previous = previous and line[4:] == '/dev/null'
                elif line.startswith('@@ ') and (selected or previous):
                    match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
                    if match:
                        position = {'old_position': int(match[1]) if previous else 0,
                                    'new_position': int(match[3]) if selected else 0}
                        break
        if 'new_position' in action and 'old_position' in action and not action.get('conversation'):
            position = {key: action[key] for key in ('new_position', 'old_position')}
        return position

    def create_finding(self, action, failures):
        position = self.position(action)
        path = action.get('path') or ''
        body = self.body(action['body'])
        marker = '<!--tauceti-source-finding:' + hashlib.sha256(json.dumps(
            [self.source.identity.record(), self.number, self.head, action['rubric'], action.get('run_id')],
            sort_keys=True).encode()).hexdigest() + '-->'
        body = marker + '\n' + body
        if position is None:
            self.api.request('POST', f'{self.prefix}/issues/{self.number}/comments',
                             {'body': f'<!--tauceti-source-head:{self.head}-->\n' + body})
        else:
            self.api.request('POST', f'{self.prefix}/pulls/{self.number}/reviews',
                             dict(event='COMMENT', commit_id=self.head, body=marker,
                                  comments=[dict(body=body, path=path, **position)]))
        roots = self.roots()
        exact = [root for root in roots if marker in root.get('body', '') and root['login'] == self.identity()
                 and root['head_sha'] == self.head]
        if not exact:
            raise AmbiguousWrite('native finding requires confirmed read-back')
        return max(exact, key=lambda root: root['id'])

    def board_comments(self):
        rows = self.source.comments(self.number)
        return sorted([dict(id=row['id'], login=self.principal(row.get('user') or {}),
                            updated_at=row.get('updated_at', ''), body=row.get('body', ''))
                       for row in rows if '<!--tauceti-scoreboard-->' in row.get('body', '')],
                      key=lambda row: (row['updated_at'], row['id']), reverse=True)

    def comment(self, method, identifier, body_file=None, failures=None, action=''):
        endpoint = (f'{self.prefix}/issues/{self.number}/comments' if method == 'POST'
                    else f'{self.prefix}/issues/comments/{identifier}')
        result = self.api.request(method, endpoint, {'body': self.body(body_file)} if body_file else None)
        if method in {'POST', 'PATCH'}:
            expected = self.body(body_file)
            exact = [row for row in self.board_comments() if row['login'] == self.identity()
                     and row['body'] == expected and (method == 'POST' or row['id'] == identifier)]
            if not exact:
                raise AmbiguousWrite('native scoreboard requires confirmed read-back')
            return max(exact, key=lambda row: row['id'])
        return result

    def replied(self, rubric, through, me):
        return self.helpers.reply_seen(self.source.findings(self.number), rubric, through, me, self.principal)

    def reply(self, parent, action, failures):
        from tauceti_tools.review_source import reply_to_finding

        return reply_to_finding(self.api, self.repository, self.number, parent, self.body(action['body']))

    def resolve(self, identifier):
        # Forgejo has no equivalent of GitHub's GraphQL resolveReviewThread.
        # The shared poster has already updated the finding with its close note.
        return None



def transport_for(repo, number, head, plan, helpers):
    source = source_for(repo)
    cls = NativePublication if hasattr(source, 'api') else GitHubPublication
    arguments = dict(source=source)
    return cls(repo, number, head, plan, helpers, **arguments)
