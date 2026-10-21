from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


@unittest.skipUnless(shutil.which('jar'), 'Requires the JDK jar tool used by the API build')
class GradleCacheTests(unittest.TestCase):
    def test_invalid_distribution_removed_without_discarding_valid_cache(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / 'src/docker/api.Dockerfile').read_text()
        # Execute the real preflight from the build's cached RUN, before Gradle starts.
        start = dockerfile.index('    set -eu;', dockerfile.index('target=/root/.gradle'))
        preflight = dockerfile[start:dockerfile.index('for project in', start)].replace('\\\n', ' ')
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / '.gradle'
            dist = cache / 'wrapper/dists/gradle-7.5.1-bin/hash'
            dist.mkdir(parents=True)
            broken = dist / 'gradle-7.5.1-bin.zip'
            broken.write_bytes(b'PK\x03\x04incomplete archive')
            marker = Path(str(broken) + '.ok')
            marker.touch()
            valid = cache / 'wrapper/dists/gradle-other-bin/hash/gradle-other-bin.zip'
            valid.parent.mkdir(parents=True)
            with zipfile.ZipFile(valid, 'w') as archive:
                archive.writestr('gradle/bin/gradle', 'valid archive fixture')
            dependency = cache / 'caches/modules-2/dependency.jar'
            dependency.parent.mkdir(parents=True)
            dependency.write_bytes(b'cached dependency')
            before = valid.read_bytes()
            failure = subprocess.run(['jar', 'tf', str(broken)], capture_output=True, text=True)
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn('zip END header not found', failure.stderr)
            subprocess.run(['sh', '-eu', '-c', preflight.replace('/root/.gradle', str(cache))], check=True)
            self.assertFalse(broken.exists(), 'Invalid distribution must be removed before Gradle reuses it')
            self.assertFalse(marker.exists(), 'Invalid distribution completion marker must be removed')
            self.assertEqual(valid.read_bytes(), before)
            self.assertEqual(dependency.read_bytes(), b'cached dependency')
            # Repeating the preflight and starting with an empty cache are both safe.
            subprocess.run(['sh', '-eu', '-c', preflight.replace('/root/.gradle', str(cache))], check=True)
            subprocess.run(['sh', '-eu', '-c', preflight.replace('/root/.gradle', str(Path(directory) / 'empty'))], check=True)

    def test_bundled_dockerfile_matches_source_and_locks_cache(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'src/docker/api.Dockerfile').read_bytes()
        self.assertIn(b'target=/root/.gradle,sharing=locked', source)
        self.assertEqual(source, (root / 'extension/bundled/docker/api.Dockerfile').read_bytes())


if __name__ == '__main__':
    unittest.main()
