import importlib.util
import os
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('feature_env', Path(__file__).parents[1] / 'src/feature-env.py')
feature = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feature)


class RoutingTests(unittest.TestCase):
    def test_deploy_and_restore_restart_only_affected_routers(self):
        cases = [
            (['proxy'], ['hrbcweblb']),
            (['ui'], ['hrbcweblb']),
            (['web'], ['hrbcweblb']),
            (['api'], ['hrbcprivateapicore']),
            (['ui', 'proxy', 'web'], ['hrbcweblb']),
            (['api', 'proxy'], ['hrbcprivateapicore', 'hrbcweblb']),
            (list(feature.SERVICES), ['hrbcprivateapicore', 'hrbcweblb']),
        ]
        for command in ('up', 'restore'):
            for targets, routers in cases:
                with self.subTest(command=command, targets=targets), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    root = Path(directory)
                    state_dir = root / 'state'
                    state_dir.mkdir()
                    env_file = root / '.env'
                    env_file.touch()
                    state = {'project': 'custom', 'source': 'custom'}
                    feature.save(state_dir / 'state.json', state)
                    feature.save(state_dir / 'feature.json', {'services': {
                        service: {'image': 'local/' + key} for key, service in feature.SERVICES.items()
                    }})
                    stack.enter_context(patch.object(feature, 'STATE', state_dir))
                    stack.enter_context(patch.dict(os.environ, {'FEATURE_ENV_STATE_DIR': str(state_dir)}))
                    stack.enter_context(patch('sys.argv', ['feature-env.py', command, '--env-file', str(env_file), '--only', *targets]))
                    stack.enter_context(patch.object(feature, 'cluster_release', return_value='9-4-0'))
                    stack.enter_context(patch.object(feature, 'ensure_current_baseline', return_value=state))
                    for name in ('load_environment', 'heal_stray_renames', 'check_build_release',
                                 'check_redirects', 'start_dependencies', 'summary'):
                        stack.enter_context(patch.object(feature, name))
                    refresh = stack.enter_context(patch.object(feature, 'refresh_web_routing'))
                    run = stack.enter_context(patch.object(feature, 'run'))
                    stack.enter_context(redirect_stdout(StringIO()))
                    # main normally owns this lock until the CLI process exits.
                    open_path = Path.open
                    def managed_open(path, *args, **kwargs):
                        handle = open_path(path, *args, **kwargs)
                        if path == state_dir.with_name('state.lock'):
                            stack.callback(handle.close)
                        return handle
                    stack.enter_context(patch.object(Path, 'open', managed_open))
                    previous_umask = os.umask(0o077)
                    stack.callback(os.umask, previous_umask)
                    feature.main()
                    restarts = [call.args[0] for call in run.call_args_list if call.args[0][:2] == ['docker', 'restart']]
                    self.assertEqual(restarts, [['docker', 'restart', *['custom-' + router + '-1' for router in routers]]])
                    self.assertEqual(refresh.call_count, int('web' in targets))


if __name__ == '__main__':
    unittest.main()
