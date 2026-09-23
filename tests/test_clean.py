import importlib.util
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('feature_env', Path(__file__).parents[1] / 'src/feature-env.py')
feature = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feature)


class CleanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_dir = Path(directory.name)
        patcher = patch.object(feature, 'STATE', self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = {'project': 'hrbc1'}

    def clean(self, targets, images, running):
        commands = []
        def run(args, **kwargs):
            commands.append([str(a) for a in args])
            return subprocess.CompletedProcess(args, 0)
        with patch.object(feature, 'output', return_value='\n'.join(images)), \
                patch.object(feature, 'inspect', side_effect=lambda name: {'Config': {'Image': running[name]}}), \
                patch.object(feature.subprocess, 'run', side_effect=run), redirect_stdout(StringIO()):
            feature.clean(self.state, targets, keep_builds=2, dry_run=False)
        return commands

    def test_selected_clean_keeps_other_targets_undeployed_builds(self):
        feature.save(self.state_dir / 'feature.json', {'services': {
            'hrbcui': {'image': 'local/hrbc1-ui:new'}, 'hrbcproductweb': {'image': 'local/hrbc1-web:built'}}})
        running = {'hrbc1-hrbcui-1': 'local/hrbc1-ui:new', 'hrbc1-hrbcproductweb-1': 'registry/web:H9.5.0'}
        commands = self.clean(['ui'], ['local/hrbc1-ui:new', 'local/hrbc1-ui:old', 'local/hrbc1-web:built'], running)
        self.assertEqual(commands, [['docker', 'rmi', 'local/hrbc1-ui:old']])

    def test_state_write_is_private_and_leaves_no_temporary_file(self):
        path = self.state_dir / 'state.json'
        feature.save(path, {'a': 1})
        feature.save(path, {'a': 2})
        self.assertEqual(json.loads(path.read_text()), {'a': 2})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.state_dir.iterdir()], ['state.json'])


if __name__ == '__main__':
    unittest.main()
