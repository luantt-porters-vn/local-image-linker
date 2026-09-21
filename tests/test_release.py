import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('feature_env', Path(__file__).parents[1] / 'src/feature-env.py')
feature = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feature)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.state_patch = patch.object(feature, 'STATE', self.state_dir)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.state = {'project': 'hrbc1', 'source': 'hrbc1', 'originals': {
            'hrbcproductweb': 'registry/hrbc/product-web:runtime-H9.4.0'}}
        self.args = SimpleNamespace(command='build', release='9-5-0')

    def test_auto_detects_future_release_from_unmodified_office(self):
        with patch.object(feature, 'inspect', return_value={'Config': {'Image': 'registry/hrbc/tools:runtime-H10.2.0'}}) as inspect:
            self.assertEqual(feature.cluster_release('custom', ''), '10-2-0')
            self.assertEqual(feature.cluster_release('custom', 'auto'), '10-2-0')
            inspect.assert_called_with('custom-hrbcoffice-1')

    def test_explicit_stale_release_is_rejected(self):
        with patch.object(feature, 'inspect', return_value={'Config': {'Image': 'registry/hrbc/tools:runtime-H9.5.0'}}):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                feature.cluster_release('hrbc1', '9-4-0')

    def test_unrecognised_office_tag_does_not_guess(self):
        with patch.object(feature, 'inspect', return_value={'Config': {'Image': 'registry/hrbc/tools:latest'}}):
            with self.assertRaisesRegex(ValueError, 'Cannot detect'):
                feature.cluster_release('hrbc1')

    def test_capture_rejects_mixed_release_and_local_baselines(self):
        feature.validate_original_image('hrbcproductweb', 'registry/product-web:runtime-H9.5.0', '9-5-0')
        with self.assertRaisesRegex(ValueError, 'finish the cluster upgrade'):
            feature.validate_original_image('hrbcproductweb', 'registry/product-web:runtime-H9.4.0', '9-5-0')
        with self.assertRaisesRegex(ValueError, 'published cluster'):
            feature.validate_original_image('hrbcui', 'local/hrbc1-ui:feature-old', '9-5-0')

    def test_existing_state_release_inferred_without_modification(self):
        self.args.release = '9-4-0'
        with patch.object(feature, 'prepare') as prepare:
            self.assertIs(feature.ensure_current_baseline(self.args, self.state), self.state)
            prepare.assert_not_called()

    def test_upgrade_with_old_local_apps_preserves_baseline(self):
        feature.save(self.state_dir / 'state.json', self.state)
        before = (self.state_dir / 'state.json').read_bytes()
        with patch.object(feature, 'inspect', return_value={'Config': {'Image': 'local/hrbc1-web:old'}}):
            with self.assertRaisesRegex(ValueError, 'published application images'):
                feature.ensure_current_baseline(self.args, self.state)
        self.assertEqual((self.state_dir / 'state.json').read_bytes(), before)

    def test_deploy_and_restore_cannot_use_stale_baseline(self):
        for command in ('up', 'restore'):
            self.args.command = command
            with self.assertRaisesRegex(ValueError, 'Run Build'):
                feature.ensure_current_baseline(self.args, self.state)

    def test_refresh_backs_up_state_and_invalidates_old_builds(self):
        for name in ('state', 'normal', 'build', 'feature', 'routing'):
            feature.save(self.state_dir / (name + '.json'), self.state if name == 'state' else {'old': name})
        replacement = {**self.state, 'release': '9-5-0'}
        def capture(args):
            feature.save(feature.STATE / 'state.json', replacement)
            feature.save(feature.STATE / 'normal.json', {'new': True})
        with patch.object(feature, 'inspect', return_value={'Config': {'Image': 'registry/published:9.5'}}), patch.object(feature, 'prepare', side_effect=capture):
            self.assertEqual(feature.ensure_current_baseline(self.args, self.state), replacement)
        backup, = self.state_dir.glob('backup-release-*')
        self.assertEqual(json.loads((backup / 'state.json').read_text()), self.state)
        for name in ('build', 'feature', 'routing'):
            self.assertTrue((backup / (name + '.json')).exists())
            self.assertFalse((self.state_dir / (name + '.json')).exists())
        self.assertEqual(json.loads((self.state_dir / 'normal.json').read_text()), {'new': True})
        self.assertEqual(feature.STATE, self.state_dir)

    def test_failed_capture_leaves_active_state_unchanged(self):
        feature.save(self.state_dir / 'state.json', self.state)
        with patch.object(feature, 'inspect', return_value={'Config': {'Image': 'registry/published:9.5'}}), patch.object(feature, 'prepare', side_effect=ValueError('capture failed')):
            with self.assertRaisesRegex(ValueError, 'capture failed'):
                feature.ensure_current_baseline(self.args, self.state)
        self.assertEqual(json.loads((self.state_dir / 'state.json').read_text()), self.state)
        self.assertEqual(feature.STATE, self.state_dir)
        self.assertFalse(list(self.state_dir.glob('backup-release-*')))

    def test_partial_build_does_not_allow_other_release_to_deploy(self):
        feature.save(self.state_dir / 'build.json', {'releases': {'web': '9-5-0', 'ui': '9-4-0'}})
        feature.check_build_release(['web'], '9-5-0')
        with self.assertRaisesRegex(ValueError, 'Rebuild ui'):
            feature.check_build_release(['web', 'ui'], '9-5-0')

    def test_legacy_build_without_release_requires_rebuild(self):
        feature.save(self.state_dir / 'build.json', {'images': {'web': 'local/web:old'}})
        with self.assertRaisesRegex(ValueError, 'Rebuild web'):
            feature.check_build_release(['web'], '9-5-0')


if __name__ == '__main__':
    unittest.main()
