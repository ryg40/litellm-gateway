"""Exercise stable discovery, pin consistency, and private PR publication offline."""
import ast
import base64
from contextlib import redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'update-upstream.py'
spec = importlib.util.spec_from_file_location('updater', SCRIPT)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
OLD = 'sha256:' + '1' * 64
NEW = 'sha256:' + '2' * 64


@unittest.skipUnless(shutil.which('git'), 'Git is not in the gateway test image')
class UpstreamUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(updater, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.git('init', '-q', '-b', 'local-dev')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'core.hooksPath', '/dev/null')
        for name in updater.FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Current version 1.2.3 ' + OLD + '\n')
        (self.root / 'Dockerfile').write_text(
            'ARG LITELLM_VERSION=1.2.3\nARG BASE_IMAGE=' + updater.IMAGE + '@' + OLD + '\nARG BASE_DIGEST=' + OLD + '\n')
        (self.root / 'docker-bake.hcl').write_text(
            'variable "LITELLM_VERSION" {\n  default = "1.2.3"\n}\n'
            'variable "BASE_IMAGE" {\n  default = "' + updater.IMAGE + '@' + OLD + '"\n}\n')
        (self.root / 'image/hooks/litellm_versions.py').write_text('TESTED_VERSIONS = ("1.2.2", "1.2.3")\n')
        (self.root / '.gitignore').write_text('.local/\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.git('remote', 'add', 'origin', 'https://forge.example/team/gateway.git')
        self.env = patch.dict('os.environ', {
            'CI_SERVER_URL': 'https://forge.example', 'CI_REPOSITORY': 'team/gateway', 'CI_TOKEN': 'synthetic'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def prepare(self, version='1.2.4', digest=NEW):
        real_run = updater.run

        def run(*args, **kwargs):
            if args[:3] == ('docker', 'buildx', 'imagetools'):
                return json.dumps({'digest': digest, 'manifests': [
                    {'platform': {'os': 'linux', 'architecture': arch}} for arch in ['amd64', 'arm64']]})
            if args[:2] == ('python3', 'scripts/build-architecture-html.py'):
                (self.root / 'docs/architecture.html').write_text('Built for ' + version + '\n')
                return ''
            return real_run(*args, **kwargs)

        release = {'tag_name': 'v' + version, 'draft': False, 'prerelease': False}
        with patch.object(updater, 'request_json', return_value=release), patch.object(updater, 'run', side_effect=run):
            updater.prepare()
        return json.loads((self.root / updater.REPORT).read_text())

    def publish(self, repository=None, pulls=None, existing_branch=False, post_error=False):
        real_run = updater.run
        calls = []
        api_calls = []
        repository = repository or {'private': True, 'default_branch': 'local-dev'}

        def run(*args, **kwargs):
            calls.append(args)
            if args[:2] in [('git', 'ls-remote'), ('git', 'commit')] or 'push' in args:
                env = kwargs['env']
                self.assertEqual(env['GIT_CONFIG_COUNT'], '1')
                self.assertEqual(env['GIT_CONFIG_KEY_0'], 'http.https://forge.example/.extraheader')
                expected = 'Authorization: Basic ' + base64.b64encode(b'x-access-token:synthetic').decode()
                origin = self.git('remote', 'get-url', '--push', 'origin')
                self.assertEqual(real_run('git', 'config', '--get-urlmatch', 'http.extraheader', origin, env=env), expected)
            if args[:2] == ('git', 'ls-remote'):
                return 'existing' if existing_branch else ''
            if args[:2] == ('git', '-c'):
                return ''
            return real_run(*args, **kwargs)

        def api(url, token=None, data=None):
            api_calls.append((url, data))
            if data is not None:
                if post_error:
                    raise OSError('lost response')
                return {'number': 7}
            if '/pulls?' in url:
                return pulls or []
            return repository

        with patch.object(updater, 'request_json', side_effect=api), patch.object(updater, 'run', side_effect=run):
            updater.publish()
        return calls, api_calls

    def test_upgrade_changes_both_pins_and_keeps_old_tested_versions(self):
        report = self.prepare()
        self.assertTrue(report['changed'])
        self.assertEqual(updater.current_pin(), ('1.2.4', NEW))
        self.assertIn('("1.2.2", "1.2.3", "1.2.4")', (self.root / 'image/hooks/litellm_versions.py').read_text())
        self.assertEqual(report['source'], self.git('rev-parse', 'HEAD'))

    def test_equal_or_older_release_changes_nothing(self):
        for version in ['1.2.3', '1.2.2']:
            with self.subTest(version=version):
                self.assertFalse(self.prepare(version)['changed'])
                self.assertEqual(self.git('status', '--porcelain'), '')

    def test_prerelease_and_invalid_versions_fail(self):
        for tag in ['v1.2.4-rc1', 'v01.2.4', 'main', 'v1.2.4\ninjected']:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                updater.release_version({'tag_name': tag, 'draft': False, 'prerelease': False})
        with self.assertRaises(ValueError):
            updater.release_version({'tag_name': 'v1.2.4', 'draft': True, 'prerelease': False})

    def test_bad_digest_or_missing_platform_fails_before_writes(self):
        with self.assertRaises(ValueError):
            self.prepare(digest='sha256:bad')
        self.assertEqual(self.git('status', '--porcelain'), '')
        with self.assertRaises(ValueError):
            updater.image_digest({'digest': NEW, 'manifests': [{'platform': {'os': 'linux', 'architecture': 'amd64'}}]})

    def test_dirty_checkout_and_mismatched_pins_fail(self):
        (self.root / 'README.md').write_text('modified')
        with self.assertRaises(ValueError):
            self.prepare()
        (self.root / 'docker-bake.hcl').write_text('variable "BASE_IMAGE" { default = "wrong" }')
        with self.assertRaises(ValueError):
            updater.current_pin()

    def test_publish_origin_with_git_suffix_uses_exact_branch_without_tags_and_opens_one_pr(self):
        self.prepare()
        calls, api_calls = self.publish()
        pushes = [args for args in calls if 'push' in args]
        self.assertEqual(pushes, [('git', '-c', 'push.followTags=false', 'push', 'origin',
                                   'HEAD:refs/heads/automation/litellm-v1.2.4')])
        posts = [data for _, data in api_calls if data is not None]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['base'], 'local-dev')
        self.assertEqual(posts[0]['head'], 'automation/litellm-v1.2.4')
        self.assertFalse(any('--force' in arg for args in calls for arg in args))

    def test_publish_origin_without_git_suffix_passes(self):
        self.git('remote', 'set-url', 'origin', 'https://forge.example/team/gateway')
        self.prepare()
        calls, api_calls = self.publish()
        self.assertEqual(sum('push' in args for args in calls), 1)
        self.assertEqual(sum(data is not None for _, data in api_calls), 1)
        self.assertEqual(self.git('log', '-1', '--format=%s'), 'Update LiteLLM base to 1.2.4')

    def test_publish_rejects_different_origin_before_commit_or_network_git(self):
        self.prepare()
        source = self.git('rev-parse', 'HEAD')
        for origin in [
            'https://forge.example/team/another.git',
            'https://different.example/team/gateway.git',
            'https://forge.example/another/gateway.git',
            'https://forge.example/team/gateway.git.git',
            'https://forge.example/team/gateway/',
            'https://forge.example/team/gateway.git?query=1',
            'https://forge.example/team/gateway.git#fragment',
            'http://forge.example/team/gateway.git',
            'https://user@forge.example/team/gateway.git',
        ]:
            with self.subTest(origin=origin):
                self.git('remote', 'set-url', '--push', 'origin', origin)
                with patch.object(updater, 'run', wraps=updater.run) as run:
                    with self.assertRaisesRegex(updater.UpdateError, '^origin differs from the CI repository$'):
                        self.publish()
                self.assertFalse(any(call.args[:2] in [('git', 'ls-remote'), ('git', 'commit')]
                                     or 'push' in call.args for call in run.call_args_list))
                self.assertEqual(self.git('rev-parse', 'HEAD'), source)
                self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_public_repository_and_wrong_default_fail_without_push(self):
        self.prepare()
        for repo in [{'private': False, 'default_branch': 'local-dev'}, {'private': True, 'default_branch': 'portable'}]:
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                self.publish(repository=repo)
        self.assertEqual(self.git('log', '--format=%s'), 'fixture')

    def test_github_and_credential_urls_are_refused(self):
        for url in ['https://github.com', 'https://api.github.com', 'http://forge.example', 'https://user@forge.example',
                    'https://forge.example/path', 'https://forge.example?query=1']:
            with self.subTest(url=url), patch.dict('os.environ', {'CI_SERVER_URL': url}), self.assertRaises(ValueError):
                updater.api_config()

    def test_existing_open_or_closed_pr_is_not_reopened_or_pushed(self):
        self.prepare()
        for state in ['open', 'closed']:
            pull = {'number': 7, 'state': state, 'head': {'ref': 'automation/litellm-v1.2.4'}, 'base': {'ref': 'local-dev'}}
            with self.subTest(state=state):
                calls, api_calls = self.publish(pulls=[pull])
                self.assertFalse(any('push' in args for args in calls))
                self.assertFalse(any(data is not None for _, data in api_calls))

    def test_existing_orphan_branch_is_preserved(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.publish(existing_branch=True)
        self.assertEqual(self.git('log', '--format=%s'), 'fixture')

    def test_unexpected_file_or_changed_digest_is_not_published(self):
        self.prepare()
        (self.root / 'unexpected.txt').write_text('untracked')
        with self.assertRaises(ValueError):
            self.publish()
        (self.root / 'unexpected.txt').unlink()
        path = self.root / 'Dockerfile'
        path.write_text(path.read_text().replace(NEW, OLD))
        with self.assertRaises(ValueError):
            self.publish()

    def test_pull_request_pagination_and_lost_response_recovery(self):
        wanted = {'number': 7, 'state': 'open', 'head': {'ref': 'automation/litellm-v1.2.4'}, 'base': {'ref': 'local-dev'}}
        with patch.object(updater, 'request_json', side_effect=[[{}] * 50, [wanted]]) as api:
            self.assertEqual(updater.matching_pr('https://forge.example/api', 'synthetic', wanted['head']['ref']), wanted)
            self.assertIn('page=2', api.call_args[0][0])
        self.prepare()
        responses = [ {'private': True, 'default_branch': 'local-dev'}, [], OSError('lost response'), [wanted] ]
        real_run = updater.run

        def run(*args, **kwargs):
            if args[:2] == ('git', 'ls-remote') or args[:2] == ('git', '-c'):
                return ''
            return real_run(*args, **kwargs)

        with patch.object(updater, 'request_json', side_effect=responses) as api, patch.object(updater, 'run', side_effect=run):
            updater.publish()
            self.assertEqual(sum(call.kwargs.get('data') is not None or len(call.args) == 3 for call in api.call_args_list), 1)


class StopMessageTests(unittest.TestCase):
    def test_script_validation_messages_are_fixed_text(self):
        tree = ast.parse(SCRIPT.read_text())
        messages = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            name = getattr(node.exc.func, 'id', '')
            self.assertNotEqual(name, 'ValueError')
            if name == 'UpdateError':
                self.assertEqual(len(node.exc.args), 1)
                self.assertIsInstance(node.exc.args[0], ast.Constant)
                message = node.exc.args[0].value
                self.assertIsInstance(message, str)
                self.assertNotIn('://', message)
                messages.append(message)
        self.assertEqual(len(messages), 22)

    def test_main_prints_script_validation_message(self):
        output = io.StringIO()
        with patch('sys.argv', ['update-upstream.py', 'publish']), redirect_stderr(output):
            with patch.object(updater, 'publish', side_effect=lambda: updater.version_tuple('bad')):
                self.assertEqual(updater.main(), 1)
        self.assertEqual(output.getvalue(), 'upstream: expected a stable X.Y.Z version; update stopped\n')

    def test_main_hides_external_exception_text(self):
        private = 'https://token@example.invalid/private'
        for error in [ValueError(private), OSError(private), KeyError(private),
                      json.JSONDecodeError(private, '', 0), subprocess.CalledProcessError(1, private)]:
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch('sys.argv', ['update-upstream.py', 'publish']), redirect_stderr(output):
                    with patch.object(updater, 'publish', side_effect=error):
                        self.assertEqual(updater.main(), 1)
                self.assertEqual(output.getvalue(), 'upstream: ' + type(error).__name__ + '; update stopped\n')


if __name__ == '__main__':
    unittest.main()
