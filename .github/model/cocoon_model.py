#!/usr/bin/env python3
"""Cocoon-equivalent automation model for the c4a1d9 strict probe fixture.

Runs inside the fixture's base-defined `pull_request_target` workflow as github-actions[bot] (the base
checkout only; no PR code is executed). It mirrors, for a non-privileged (non-flutter-hacker) author:

flutter-dashboard  (flutter/cocoon@38acc92 app_dart/lib/src/service/pull_request_manager.dart)
  opened       -> createAwaitingCicdLabelCheckRun(head)                       (_handleOpened, else-branch)
  synchronize  -> remove CICD if present; createAwaitingCicdLabelCheckRun(head) (_handleSynchronize)
  labeled CICD -> resolveAwaitingCicdLabelCheckRun(head); schedule presubmits    (_handleLabeled)
  reopened     -> comment "a new commit is needed"; processLabels (emergency only) (_handleReopened)
  edited/closed-> no label or guard change                                       (_handleEdited / closed)
  createAwaitingCicdLabelCheckRun dedupes on (name, head SHA)                    (scheduler.dart:937)
  "schedule presubmits" is modelled as one in-progress check run that a delayed workflow completes.

auto_submit  (auto_submit/lib/validations/*, service/validation_service.dart)
  any pull_request event on an open PR carrying `autosubmit`:
  Approval (state-only, reverse-chronological, first review per reviewer, author excluded, approval group),
  CiSuccessful (statuses + check runs deduped by name/newest start; Merge Queue Guard ignored; success,
  skipped and completed-neutral pass; other completed = failure; not completed = pending), EmptyChecks
  (>=1 check run on head), Mergeable, TestExemption. Any REMOVE_LABEL failure -> remove `autosubmit`;
  pending -> retry; all pass -> squash merge with sha = current head (requestSha).
  Production merges as the auto-submit[bot] App, whose push starts `push` workflows. GITHUB_TOKEN pushes do
  not, so after a merge into release-* the model dispatches the release workflow on that branch.
Deviation recorded in the receipt: approving_reviews comes from config (fixture: 1; production: 2).
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request

GUARD = 'Awaiting CICD label'
PRESUBMIT = 'Linux presubmit (LUCI model)'
MQ_GUARD = 'Merge Queue Guard'
CICD, AUTOSUBMIT = 'CICD', 'autosubmit'
MISSING_TESTS, TEST_EXEMPT = 'missing tests', 'test exemption'


def log(tag, **kw):
    kw['tag'] = tag
    kw['t'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    print('MODEL ' + json.dumps(kw, sort_keys=True), flush=True)


class Api:
    def __init__(self, token, repo, base='https://api.github.com'):
        self.token, self.repo, self.base = token, repo, base

    def req(self, method, path, body=None):
        url = self.base + (path if path.startswith('/repos/') else f'/repos/{self.repo}{path}')
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'cocoon-model',
            **({'Content-Type': 'application/json'} if data else {})})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, (json.loads(raw) if raw else None)
        except ValueError:
            return status, None


def labels_of(pr):
    return [l['name'] for l in pr.get('labels') or []]


def check_runs(api, sha, name=None):
    q = f'/commits/{sha}/check-runs?per_page=100' + (f'&check_name={urllib.parse.quote(name, safe="")}' if name else '')
    st, d = api.req('GET', q)
    return (d or {}).get('check_runs', []) if st == 200 else []


def create_guard(api, sha):
    if check_runs(api, sha, GUARD):
        log('guard_exists', sha=sha)
        return
    st, d = api.req('POST', '/check-runs', {'name': GUARD, 'head_sha': sha, 'status': 'in_progress',
                                            'output': {'title': GUARD, 'summary': 'This PR is awaiting the CICD label to start tests.'}})
    log('guard_created', sha=sha, http=st, id=(d or {}).get('id'))


def resolve_guard(api, sha):
    for c in check_runs(api, sha, GUARD):
        st, _ = api.req('PATCH', f'/check-runs/{c["id"]}', {'status': 'completed', 'conclusion': 'success',
                                                           'output': {'title': GUARD, 'summary': 'CICD label added.'}})
        log('guard_resolved', sha=sha, http=st, id=c['id'])


def schedule_presubmits(api, sha, cfg):
    if check_runs(api, sha, PRESUBMIT):
        log('presubmit_exists', sha=sha)
        return
    st, d = api.req('POST', '/check-runs', {'name': PRESUBMIT, 'head_sha': sha, 'status': 'in_progress'})
    log('presubmit_scheduled', sha=sha, http=st, id=(d or {}).get('id'))
    if st == 201:
        st2, _ = api.req('POST', f'/actions/workflows/{cfg["luci_workflow"]}/dispatches',
                         {'ref': cfg['default_branch'], 'inputs': {'check_run_id': str(d['id']),
                                                                  'delay_seconds': str(cfg['presubmit_seconds'])}})
        log('presubmit_completion_dispatched', sha=sha, http=st2)


CLA_MODEL = 'cla/google (model)'


def cla_model(api, sha):
    # Not Cocoon: models the independent google-cla app (and the labeler/scan pull_request runs) that
    # complete on every external head without member action (live: external-head-checks.txt).
    st, d = api.req('POST', '/check-runs', {'name': CLA_MODEL, 'head_sha': sha, 'status': 'completed',
                                            'conclusion': 'success', 'output': {'title': CLA_MODEL, 'summary': 'model'}})
    log('cla_model_check', sha=sha, http=st, id=(d or {}).get('id'))


def dashboard(api, event, cfg):
    action, pr = event['action'], event['pull_request']
    head, author = pr['head']['sha'], pr['user']['login'].lower()
    privileged = author in [p.lower() for p in cfg['privileged']]
    labels = labels_of(pr)
    log('dashboard_event', action=action, head=head, base=pr['base']['ref'], author=author,
        privileged=privileged, labels=labels, label=(event.get('label') or {}).get('name'))
    if action in ('opened', 'synchronize', 'reopened'):
        cla_model(api, head)
    if action == 'opened':
        if privileged:
            api.req('POST', f'/issues/{pr["number"]}/labels', {'labels': [CICD]})
            schedule_presubmits(api, head, cfg)
        else:
            create_guard(api, head)
    elif action == 'synchronize':
        if privileged:
            (schedule_presubmits if CICD in labels else lambda a, s, *_: create_guard(a, s))(api, head, cfg)
        else:
            if CICD in labels:
                st, _ = api.req('DELETE', f'/issues/{pr["number"]}/labels/{CICD}')
                log('cicd_removed', head=head, http=st)
            create_guard(api, head)
    elif action == 'labeled' and (event.get('label') or {}).get('name') == CICD:
        resolve_guard(api, head)
        schedule_presubmits(api, head, cfg)
    elif action == 'reopened':
        st, _ = api.req('POST', f'/issues/{pr["number"]}/comments',
                        {'body': f'Model of Cocoon _warnThatANewCommitIsNeeded for {head}.'})
        log('reopened_comment_only', head=head, http=st)


def validate(api, pr, cfg):
    """Returns (verdict, details): verdict in {'merge', 'hold', 'remove'}."""
    head, author = pr['head']['sha'], pr['user']['login'].lower()
    labels = labels_of(pr)
    details = {}
    # Approval
    _, reviews = api.req('GET', f'/pulls/{pr["number"]}/reviews?per_page=100')
    group = [g.lower() for g in cfg['approval_group']]
    remaining = cfg['approving_reviews'] - (1 if author in group else 0)
    seen, approvers, changes = set(), [], []
    for r in reversed(reviews or []):
        who = r['user']['login'].lower()
        if who == author or who not in group or who in seen:
            continue
        if r['state'] == 'APPROVED':
            approvers.append((who, r['commit_id']))
            remaining -= 1
        elif r['state'] == 'CHANGES_REQUESTED':
            changes.append(who)
            remaining += 1
        if r['state'] in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            seen.add(who)
    approved = remaining <= 0 and not changes
    details['approval'] = {'approved': approved, 'approvers_and_review_commits': approvers, 'head': head}
    # CiSuccessful + EmptyChecks
    runs = [c for c in check_runs(api, head) if c['name'] not in cfg['ignore_checks']]
    newest = {}
    for c in runs:
        if c['name'] not in newest or (c.get('started_at') or '') > (newest[c['name']].get('started_at') or ''):
            newest[c['name']] = c
    failures, pending = [], []
    for name, c in newest.items():
        if name == MQ_GUARD:
            continue
        if c.get('conclusion') in ('success', 'skipped') or (c['status'] == 'completed' and c.get('conclusion') == 'neutral'):
            continue
        (failures if c['status'] == 'completed' else pending).append(name)
    _, st = api.req('GET', f'/commits/{head}/status')
    for s in (st or {}).get('statuses', []):
        if s['state'] != 'success':
            (failures if s['state'] in ('failure', 'error') else pending).append('status:' + s['context'])
    details['checks'] = {'names': sorted(newest), 'failures': failures, 'pending': pending, 'count': len(runs)}
    mergeable = pr.get('mergeable')
    details['mergeable'] = mergeable
    test_fail = MISSING_TESTS in labels and TEST_EXEMPT not in labels
    if not approved or failures or not runs or mergeable is False or test_fail:
        return 'remove', details
    if pending or mergeable is None:
        return 'hold', details
    return 'merge', details


def autosubmit(api, number, cfg, sleep=time.sleep):
    # Production processes autosubmit pub/sub messages from a cron-driven pull handler
    # (auto_submit/lib/requests/check_pull_request.dart), so evaluation lags the event; modelled by
    # initial_delay_seconds. IGNORE_TEMPORARILY leaves the message for redelivery: modelled by polling.
    sleep(cfg.get('initial_delay_seconds', 0))
    deadline = time.time() + cfg['hold_seconds']
    while True:
        _, pr = api.req('GET', f'/pulls/{number}')
        if not pr or pr['state'] != 'open':
            log('autosubmit_skip_not_open', number=number, state=(pr or {}).get('state'))
            return 'skip'
        if AUTOSUBMIT not in labels_of(pr):
            log('autosubmit_skip_no_label', number=number, labels=labels_of(pr))
            return 'skip'
        verdict, details = validate(api, pr, cfg)
        log('autosubmit_validation', number=number, head=pr['head']['sha'], base=pr['base']['ref'],
            labels=labels_of(pr), verdict=verdict, **details)
        if verdict == 'remove':
            api.req('DELETE', f'/issues/{number}/labels/{AUTOSUBMIT}')
            log('autosubmit_label_removed', number=number)
            return 'removed'
        if verdict == 'merge':
            head, base = pr['head']['sha'], pr['base']['ref']
            st, m = api.req('PUT', f'/pulls/{number}/merge', {'merge_method': 'squash', 'sha': head})
            log('autosubmit_merge', number=number, head=head, base=base, http=st, merged=(m or {}).get('merged'),
                merge_sha=(m or {}).get('sha'), message=(m or {}).get('message'))
            if st == 200 and base.startswith(cfg['release_prefix']):
                st2, _ = api.req('POST', f'/actions/workflows/{cfg["release_workflow"]}/dispatches', {'ref': base})
                log('release_dispatched', base=base, http=st2)
            return 'merged' if st == 200 else 'merge_failed'
        if time.time() > deadline:
            log('autosubmit_hold_timeout', number=number, head=pr['head']['sha'])
            return 'hold_timeout'
        sleep(cfg['poll_seconds'])


def main():
    mode = sys.argv[1]
    cfg = json.load(open(os.environ['MODEL_CONFIG']))
    api = Api(os.environ['GITHUB_TOKEN'], os.environ['GITHUB_REPOSITORY'])
    event = json.load(open(os.environ['GITHUB_EVENT_PATH']))
    if mode == 'dashboard':
        dashboard(api, event, cfg)
    elif mode == 'autosubmit':
        autosubmit(api, event['pull_request']['number'], cfg)


if __name__ == '__main__':
    main()
