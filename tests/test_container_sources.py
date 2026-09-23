from contextlib import ExitStack
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('feature_env', Path(__file__).parents[1] / 'src/feature-env.py')
feature = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feature)


class ContainerSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'tracked.js').write_text('original')
        (self.repo / 'deleted.js').write_text('delete me')
        (self.repo / '.gitignore').write_text('ignored.js\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'Initial')
        (self.repo / 'tracked.js').write_text('edited')
        (self.repo / 'deleted.js').unlink()
        for name in ['new.js', 'ignored.js', '.npmrc', '.env', 'node_modules/pkg/index.js', 'dist/app.js']:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args]).decode().strip()

    def docker_cp(self, returncode=0):
        """Serve self.repo the way `docker cp <container>:<path>/. -` streams it, recording the copy target."""
        real_popen = subprocess.Popen
        def popen(args, *rest, **kwargs):
            if args[0] != 'docker':
                return real_popen(args, *rest, **kwargs)
            self.assertEqual(args, ['docker', 'cp', 'ui-dev:/workspaces/hrbc-ui-react/.', '-'])
            buffer = io.BytesIO()
            if not returncode:
                with tarfile.open(fileobj=buffer, mode='w') as archive:
                    archive.add(self.repo, arcname='.')
                buffer.seek(0)
            return SimpleNamespace(stdout=buffer, returncode=returncode, wait=lambda: returncode)
        copy = feature.copy_container_checkout
        def record(container, source, checkout, label):
            self.copied_to = checkout
            return copy(container, source, checkout, label)
        stack = ExitStack()
        stack.enter_context(patch.object(feature.subprocess, 'Popen', side_effect=popen))
        stack.enter_context(patch.object(feature, 'copy_container_checkout', side_effect=record))
        return stack

    def test_container_snapshot_matches_local_saved_working_tree(self):
        local = self.root / 'local'
        expected = feature.snapshot(self.repo, local)
        destination = self.root / 'container'
        with self.docker_cp():
            actual = feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', destination)
        files = lambda directory: {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        self.assertEqual(files(destination), files(local))
        self.assertEqual(set(files(destination)), {'tracked.js', 'new.js', '.gitignore'})
        self.assertEqual(actual['sha'], expected['sha'])
        self.assertTrue(actual['dirty'])
        self.assertEqual(actual['repository'], 'docker://ui-dev/workspaces/hrbc-ui-react')
        self.assertFalse(self.copied_to.exists())

    def test_mixed_sources_resolve_without_docker(self):
        with patch.dict(os.environ, {'FEATURE_ENV_UI_REPO': 'docker://ui-dev/workspaces/hrbc-ui-react',
                                     'FEATURE_ENV_PROXY_REPO': str(self.repo),
                                     'FEATURE_ENV_CLIENT_REPO': str(self.repo)}, clear=True):
            paths = feature.repository_paths({}, ['ui', 'proxy'])
        self.assertEqual(paths['ui'], 'docker://ui-dev/workspaces/hrbc-ui-react')
        self.assertEqual(paths['proxy'], self.repo)
        self.assertEqual(paths['client'], self.repo)

    def test_invalid_container_sources(self):
        for value in ['docker://ui', 'docker://ui/', 'docker://ui/a/../b', 'docker://-x/repo', 'docker:///repo']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                feature.container_source(value)

    def test_volume_root_and_subdirectory(self):
        self.assertEqual(feature.volume_source('volume://hrbc-ui'), ('hrbc-ui', ''))
        self.assertEqual(feature.volume_source('volume://hrbc-ui/repo/'), ('hrbc-ui', '/repo'))
        for value in ['volume://', 'volume://ui/../repo', 'volume://ui,bad', 'volume:///repo']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                feature.volume_source(value)

    def test_volume_uses_readonly_stopped_helper_and_keeps_source_identity(self):
        actual_snapshot = feature.snapshot
        metadata = {'repository': 'docker://helper/image-linker-source/repo', 'dirty': True}
        with patch.object(feature, 'run') as run, patch.object(feature, 'output', return_value='helper\n') as output, \
                patch.object(feature, 'snapshot', return_value=metadata) as copy:
            result = actual_snapshot('volume://hrbc-ui/repo', self.root / 'out', 'runtime:image')
        command = output.call_args.args[0]
        self.assertIn('type=volume,source=hrbc-ui,target=/image-linker-source,readonly,volume-nocopy', command)
        self.assertEqual(command[:4], ['docker', 'create', '--pull', 'never'])
        self.assertIn('--read-only', command)
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertEqual(command[-1], 'runtime:image')
        copy.assert_called_once_with('docker://helper/image-linker-source/repo', self.root / 'out')
        self.assertEqual(result['repository'], 'volume://hrbc-ui/repo')
        self.assertEqual(run.call_args_list[0].args[0], ['docker', 'volume', 'inspect', 'hrbc-ui'])
        self.assertEqual(run.call_args_list[-1].args[0], ['docker', 'rm', '-v', 'helper'])

    def test_volume_helper_removed_after_copy_failure(self):
        actual_snapshot = feature.snapshot
        with patch.object(feature, 'run') as run, patch.object(feature, 'output', return_value='helper'), \
                patch.object(feature, 'snapshot', side_effect=ValueError('copy failed')), \
                self.assertRaisesRegex(ValueError, 'copy failed'):
            actual_snapshot('volume://hrbc-ui', self.root / 'out', 'runtime:image')
        self.assertEqual(run.call_args.args[0], ['docker', 'rm', '-v', 'helper'])

    def test_missing_volume_never_creates_helper(self):
        with patch.object(feature, 'run', side_effect=subprocess.CalledProcessError(1, 'inspect')), \
                patch.object(feature, 'output') as output, self.assertRaisesRegex(ValueError, 'No volume was created'):
            feature.snapshot('volume://missing', self.root / 'out', 'runtime:image')
        output.assert_not_called()

    def test_volume_resolves_without_container(self):
        with patch.dict(os.environ, {'FEATURE_ENV_UI_REPO': 'volume://hrbc-ui',
                                     'FEATURE_ENV_CLIENT_REPO': str(self.repo)}, clear=True):
            paths = feature.repository_paths({}, ['ui'])
        self.assertEqual(paths['ui'], 'volume://hrbc-ui')

    def test_missing_container_has_actionable_error_and_cleans_up(self):
        with self.docker_cp(returncode=1), self.assertRaisesRegex(ValueError, 'container must still exist'):
            feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', self.root / 'out')
        self.assertFalse(self.copied_to.exists())

    def test_linked_worktree_rejected_and_temporary_copy_removed(self):
        shutil.rmtree(self.repo / '.git')
        (self.repo / '.git').write_text('gitdir: /unavailable/worktree')
        with self.docker_cp(), self.assertRaisesRegex(ValueError, 'linked worktrees'):
            feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', self.root / 'out')
        self.assertFalse(self.copied_to.exists())

    def test_container_copy_never_writes_dependency_trees(self):
        (self.repo / 'packages/app/node_modules/dep').mkdir(parents=True)
        (self.repo / 'packages/app/node_modules/dep/index.js').write_text('dep')
        (self.repo / '.gradle/caches').mkdir(parents=True)
        written = []
        extract = tarfile.TarFile.extract
        def spy(archive, member, *args, **kwargs):
            written.append(member.name)
            return extract(archive, member, *args, **kwargs)
        with self.docker_cp(), patch.object(tarfile.TarFile, 'extract', spy):
            feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', self.root / 'out')
        self.assertIn('./tracked.js', written)
        self.assertFalse([name for name in written if 'node_modules' in name or '.gradle' in name])

    def test_escaping_link_fails_only_when_it_is_build_source(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('secret')
        (self.repo / 'ignored.js').unlink()
        (self.repo / 'ignored.js').symlink_to(outside / 'secret')
        with self.docker_cp():
            feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', self.root / 'ignored')
        self.assertFalse((self.root / 'ignored/ignored.js').exists())
        (self.repo / 'leak').symlink_to(outside, target_is_directory=True)
        with self.docker_cp(), self.assertRaisesRegex(ValueError, 'Symlink in build source requires review: .*/leak'):
            feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', self.root / 'tracked')

    def test_symlinked_directory_cannot_leak_files(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('secret')
        (self.repo / 'link').symlink_to(outside, target_is_directory=True)
        with patch.object(feature, 'output', return_value='link/secret\0'), self.assertRaisesRegex(ValueError, 'Symlink'):
            feature.snapshot(self.repo, self.root / 'out')


if __name__ == '__main__':
    unittest.main()
