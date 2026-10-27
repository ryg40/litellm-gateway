#!/usr/bin/env python3
"""Prepare a stable LiteLLM image update; publish only a private Forgejo PR."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path('.local/upstream-update.json')
FILES = (
    'Dockerfile', 'docker-bake.hcl', 'image/hooks/litellm_versions.py',
    'README.md', 'EXPLAINER.md', 'tests/README.md', 'docs/image.md',
    'docs/diagrams/architecture-graph.json', 'docs/architecture.html',
)
VERSION = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
IMAGE = 'ghcr.io/berriai/litellm'


def run(*args, env=None):
    return subprocess.check_output(args, cwd=ROOT, env=env, text=True).strip()


def request_json(url, token=None, data=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'litellm-gateway-updater'}
    if token:
        headers['Authorization'] = 'token ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = Request(url, headers=headers,
                  data=None if data is None else json.dumps(data).encode())
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(VERSION, value):
        raise ValueError('expected a stable X.Y.Z version')
    return tuple(map(int, value.split('.')))


def current_pin():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    versions = re.findall(r'^ARG LITELLM_VERSION=(\S+)$', dockerfile, re.M)
    images = re.findall(r'^ARG BASE_IMAGE=(\S+)$', dockerfile, re.M)
    digests = re.findall(r'^ARG BASE_DIGEST=(\S+)$', dockerfile, re.M)
    if len(versions) != 1 or len(images) != 1 or len(digests) != 1:
        raise ValueError('Dockerfile pin is ambiguous')
    version, image, digest = versions[0], images[0], digests[0]
    version_tuple(version)
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or image != IMAGE + '@' + digest:
        raise ValueError('Dockerfile image and digest disagree')
    bake = (ROOT / 'docker-bake.hcl').read_text()
    for key, expected in [('LITELLM_VERSION', version), ('BASE_IMAGE', image)]:
        values = re.findall(r'variable "' + key + r'"\s*\{\s*default = "([^"]+)"\s*\}', bake)
        if values != [expected]:
            raise ValueError('Dockerfile and bake pins disagree')
    return version, digest


def release_version(release):
    if release.get('draft') is not False or release.get('prerelease') is not False:
        raise ValueError('upstream release is not stable')
    tag = release.get('tag_name', '')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise ValueError('upstream release tag is invalid')
    version_tuple(tag[1:])
    return tag[1:]


def image_digest(manifest):
    digest = manifest.get('digest', '')
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('upstream index digest is invalid')
    platforms = {(item.get('platform', {}).get('os'), item.get('platform', {}).get('architecture'))
                 for item in manifest.get('manifests', [])}
    if not {('linux', 'amd64'), ('linux', 'arm64')} <= platforms:
        raise ValueError('upstream image needs linux/amd64 and linux/arm64')
    return digest


def prepare():
    if run('git', 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('prepare needs a clean checkout')
    previous, old_digest = current_pin()
    release = request_json('https://api.github.com/repos/BerriAI/litellm/releases/latest')
    version = release_version(release)
    report = {'changed': False, 'previous': previous, 'version': version,
              'source': run('git', 'rev-parse', 'HEAD')}
    if version_tuple(version) > version_tuple(previous):
        manifest = json.loads(run('docker', 'buildx', 'imagetools', 'inspect',
                                  IMAGE + ':v' + version, '--format', '{{json .Manifest}}'))
        digest = image_digest(manifest)
        replacements = {}
        for name in FILES:
            if name in ('image/hooks/litellm_versions.py', 'docs/architecture.html'):
                continue
            text = (ROOT / name).read_text()
            replacements[name] = (text.replace(previous, version)
                                  .replace(old_digest, digest)
                                  .replace(old_digest[7:15], digest[7:15]))
        name = 'image/hooks/litellm_versions.py'
        text = (ROOT / name).read_text()
        match = re.search(r'^TESTED_VERSIONS = (\([^\n]+\))$', text, re.M)
        if match is None or '"' + previous + '"' not in match[1]:
            raise ValueError('tested-version list is ambiguous')
        if '"' + version + '"' not in match[1]:
            replacements[name] = text[:match.start(1)] + match[1][:-1] + ', "' + version + '")' + text[match.end(1):]
        for name, text in replacements.items():
            (ROOT / name).write_text(text)
        run('python3', 'scripts/build-architecture-html.py')
        report.update(changed=True, digest=digest)
    path = ROOT / REPORT
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


def api_config():
    server = os.environ.get('CI_SERVER_URL', '').rstrip('/')
    repo = os.environ.get('CI_REPOSITORY', '')
    token = os.environ.get('CI_TOKEN', '')
    parsed = urlsplit(server)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.path or parsed.query or parsed.fragment or
            parsed.hostname.lower() in ('github.com', 'api.github.com')):
        raise ValueError('publish needs a private Forgejo HTTPS server')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not token:
        raise ValueError('CI_REPOSITORY and CI_TOKEN are required')
    return server, repo, token


def matching_pr(api, token, branch):
    page = 1
    while True:
        pulls = request_json(api + '/pulls?state=all&limit=50&page=' + str(page), token)
        if not isinstance(pulls, list):
            raise ValueError('unexpected pull request response')
        for pull in pulls:
            if pull.get('head', {}).get('ref') == branch and pull.get('base', {}).get('ref') == 'local-dev':
                return pull
        if len(pulls) < 50:
            return None
        page += 1


def publish():
    report = json.loads((ROOT / REPORT).read_text())
    if report.get('changed') is not True:
        print('upstream: no new stable release')
        return
    version = report['version']
    version_tuple(version)
    server, repo, token = api_config()
    api = server + '/api/v1/repos/' + repo
    repository = request_json(api, token)
    if repository.get('private') is not True or repository.get('default_branch') != 'local-dev':
        raise ValueError('publish requires private repository with default local-dev')
    branch = 'automation/litellm-v' + version
    prior = matching_pr(api, token, branch)
    if prior:
        print('upstream: existing pull request ' + str(prior['number']) + ' (' + prior['state'] + ')')
        return
    if run('git', 'rev-parse', 'HEAD') != report['source']:
        raise ValueError('source moved after preparation')
    if run('git', 'symbolic-ref', '--short', 'HEAD') != 'local-dev':
        raise ValueError('publish needs local-dev checkout')
    if run('git', 'diff', '--cached', '--name-only'):
        raise ValueError('publish refuses an existing staged change')
    if run('git', 'ls-files', '--others', '--exclude-standard'):
        raise ValueError('publish refuses untracked files')
    changed = run('git', 'diff', '--name-only').splitlines()
    if not changed or not set(changed) <= set(FILES):
        raise ValueError('publish refuses changes outside the update file set')
    if current_pin() != (version, report['digest']):
        raise ValueError('prepared pin changed before publication')
    url = server + '/' + repo + '.git'
    if run('git', 'remote', 'get-url', '--push', 'origin') != url:
        raise ValueError('origin differs from the CI repository')
    auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
    env = os.environ.copy()
    env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.' + server + '/.extraheader',
               GIT_CONFIG_VALUE_0='Authorization: Basic ' + auth,
               GIT_AUTHOR_NAME='litellm-gateway updater', GIT_AUTHOR_EMAIL='updater@litellm-gateway.invalid',
               GIT_COMMITTER_NAME='litellm-gateway updater', GIT_COMMITTER_EMAIL='updater@litellm-gateway.invalid')
    if run('git', 'ls-remote', 'origin', 'refs/heads/' + branch, env=env):
        raise ValueError('update branch exists without a pull request; inspect it before retrying')
    run('git', 'add', '--', *changed)
    run('git', 'commit', '-m', 'Update LiteLLM base to ' + version, env=env)
    run('git', '-c', 'push.followTags=false', 'push', 'origin', 'HEAD:refs/heads/' + branch, env=env)
    body = ('Update the pinned upstream image from ' + report['previous'] + ' to ' + version + '.\n\n'
            'Index: `' + report['digest'] + '` (linux/amd64 and linux/arm64).\n\n'
            'The daily job passes the image test suite, image checks, Compose checks, '
            'shell tests, and publication tree scans before opening this pull request.\n\n'
            'Review the hook changes: https://github.com/BerriAI/litellm/compare/v' + report['previous'] + '...v' + version + '\n\n'
            'Merge and portable promotion remain manual. This job deploys no service.')
    try:
        pull = request_json(api + '/pulls', token, {
            'base': 'local-dev', 'head': branch, 'title': 'Update LiteLLM base to ' + version, 'body': body})
    except Exception:
        # A lost response can follow a successful POST. Never repeat that POST blindly.
        pull = matching_pr(api, token, branch)
        if pull is None:
            raise
    print('upstream: pull request ' + str(pull['number']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'publish'])
    args = parser.parse_args()
    try:
        {'prepare': prepare, 'publish': publish}[args.command]()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print('upstream: ' + type(error).__name__ + '; update stopped', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
