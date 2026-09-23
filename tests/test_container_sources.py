import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
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

    def copy_from_docker(self, args, **kwargs):
        self.assertEqual(args[:3], ['docker', 'cp', 'ui-dev:/workspaces/hrbc-ui-react/.'])
        self.copied_to = Path(args[3])
        shutil.copytree(self.repo, self.copied_to, dirs_exist_ok=True, symlinks=True)

    def test_container_snapshot_matches_local_saved_working_tree(self):
        local = self.root / 'local'
        expected = feature.snapshot(self.repo, local)
        destination = self.root / 'container'
        actual_run = feature.run
        def run(args, **kwargs):
            if args[0] == 'docker':
                return self.copy_from_docker(args, **kwargs)
            return actual_run(args, **kwargs)
        with patch.object(feature, 'run', side_effect=run):
            actual = feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', destination)
        files = lambda directory: {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        self.assertEqual(files(destination), files(local))
        self.assertEqual(set(files(destination)), {'tracked.js', 'new.js', '.gitignore'})
        self.assertEqual(actual['sha'], expected['sha'])
        self.assertTrue(actual['dirty'])
        self.assertEqual(actual['repository'], 'docker://ui-dev/workspaces/hrbc-ui-react')
        self.assertFalse(self.copied_to.exists())

    def test_nested_build_and_dist_source_is_kept_but_root_output_is_not(self):
        for name in ['src/build/Tool.java', 'lib/dist/index.js', 'build/output.js']:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        feature.snapshot(self.repo, self.root / 'out')
        copied = {str(p.relative_to(self.root / 'out').as_posix()) for p in (self.root / 'out').rglob('*') if p.is_file()}
        self.assertLessEqual({'src/build/Tool.java', 'lib/dist/index.js'}, copied)
        self.assertFalse({'build/output.js', 'dist/app.js'} & copied)

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
        def fail(args, **kwargs):
            self.copied_to = Path(args[3])
            raise subprocess.CalledProcessError(1, args)
        with patch.object(feature, 'run', side_effect=fail), self.assertRaisesRegex(ValueError, 'container must still exist'):
            feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', self.root / 'out')
        self.assertFalse(self.copied_to.exists())

    def test_linked_worktree_rejected_and_temporary_copy_removed(self):
        shutil.rmtree(self.repo / '.git')
        (self.repo / '.git').write_text('gitdir: /unavailable/worktree')
        with patch.object(feature, 'run', side_effect=self.copy_from_docker), self.assertRaisesRegex(ValueError, 'linked worktrees'):
            feature.snapshot('docker://ui-dev/workspaces/hrbc-ui-react', self.root / 'out')
        self.assertFalse(self.copied_to.exists())

    def test_symlinked_directory_cannot_leak_files(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('secret')
        (self.repo / 'link').symlink_to(outside, target_is_directory=True)
        with patch.object(feature, 'output', return_value='link/secret\0'), self.assertRaisesRegex(ValueError, 'Symlink'):
            feature.snapshot(self.repo, self.root / 'out')


if __name__ == '__main__':
    unittest.main()
