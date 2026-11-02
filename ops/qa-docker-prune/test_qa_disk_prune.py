import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('prune', Path(__file__).with_name('qa-disk-prune.py'))
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(prune, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.old = self.root / 'old-cache.tar'
        self.old.write_bytes(b'disposable cache')
        os.utime(self.old, (time.time() - 90000,) * 2)

    def test_old_recent_open_mount_symlink(self):
        # intent: age, open descendants, mount ownership and links each independently retain data.
        recent = self.root / 'recent.tar'
        recent.touch()
        link = self.root / 'link.tar'
        link.symlink_to(self.old)
        with patch.object(prune.subprocess, 'run', return_value=Mock(returncode=1)):
            self.assertTrue(prune.safe(self.old, set(), set(), time.time()))
            self.assertFalse(prune.safe(recent, set(), set(), time.time()))
            self.assertFalse(prune.safe(link, set(), set(), time.time()))
            self.assertFalse(prune.safe(self.old, {str(self.old)}, set(), time.time()))
            self.assertFalse(prune.safe(self.old, set(), {str(self.root)}, time.time()))

    def test_fuser_unknown_retains(self):
        # intent: an activity refusal cannot authorize deletion.
        with patch.object(prune.subprocess, 'run', return_value=Mock(returncode=2)):
            self.assertFalse(prune.safe(self.old, set(), set(), time.time()))

    def test_lsof_failure_retains_all(self):
        # intent: incomplete activity inventory prevents all filesystem reclamation.
        with patch.object(prune, 'command', side_effect=subprocess.CalledProcessError(1, ['lsof'])):
            with self.assertRaisesRegex(RuntimeError, 'OPEN_SCAN_FAILED'):
                prune.activity()

    def test_real_removal_only_old_closed(self):
        # intent: exercise the real deletion boundary and count a single removed cache file.
        recent = self.root / 'recent.tar'
        recent.touch()
        with patch.object(prune, 'root_state', return_value=(80, 100)), \
             patch.object(prune, 'activity', return_value=(set(), set(), False, [])), \
             patch.object(prune, 'docker_cleanup'), \
             patch.object(prune, 'candidates', return_value=[self.old, recent]), \
             patch.object(prune.subprocess, 'run', return_value=Mock(returncode=1)), \
             patch.object(prune, 'command', return_value='df'):
            prune.run(False)
        self.assertFalse(self.old.exists())
        self.assertTrue(recent.exists())

    def test_threshold_uses_reserved_blocks(self):
        # intent: the cleanup threshold agrees with df even with ext4 reserved space.
        with patch.object(prune.os, 'statvfs', return_value=Mock(f_blocks=100, f_bfree=30, f_bavail=25, f_frsize=1)):
            used, available = prune.root_state()
        self.assertAlmostEqual(used, 100 * 70 / 95)
        self.assertEqual(available, 25)

    def test_no_pressure_no_activity_scan(self):
        # intent: a healthy filesystem produces zero cleanup operations.
        with patch.object(prune, 'root_state', return_value=(69, 100)), patch.object(prune, 'activity') as scan:
            prune.run(False)
            scan.assert_not_called()

    def test_overlap_no_removals(self):
        # intent: two timer invocations cannot race deletion.
        with patch('builtins.open', unittest.mock.mock_open()), \
             patch.object(prune.fcntl, 'flock', side_effect=BlockingIOError), \
             patch.object(prune, 'run') as run, patch('sys.argv', ['prune']):
            prune.main()
            run.assert_not_called()

    def test_active_build_skips_docker(self):
        # intent: Docker cache pruning cannot disrupt an active local image build.
        with patch.object(prune, 'command') as command:
            prune.docker_cleanup(True, [], False)
            command.assert_not_called()

    def test_docker_has_no_volume_ops_and_preserves_local_ci(self):
        # intent: unused CI base images and database volumes survive maintenance.
        image = {'Id': 'local', 'RepoTags': ['limitless-data-ci:py310'], 'Created': '2020-01-01T00:00:00Z'}
        with patch.object(prune, 'inspect_objects', return_value=[image]), patch.object(prune, 'command', side_effect=['local', '', '', '']) as command:
            prune.docker_cleanup(False, [], False)
        calls = [c.args[0] for c in command.call_args_list]
        self.assertFalse(any('volume' in a for a in calls))
        self.assertFalse(any('rm' in a for a in calls))
        self.assertIn(['docker', 'builder', 'prune', '-f', '--filter', 'until=24h'], calls)
        self.assertTrue(any('label!=com.docker.compose.project' in call for call in calls))


    def test_running_node_owner_keeps_dependencies(self):
        # intent: JS modules remain available for a service's next lazy import.
        dep = self.root / 'service/node_modules'
        dep.mkdir(parents=True)
        os.utime(dep, (time.time() - 90000,) * 2)
        with patch.object(prune.subprocess, 'run', return_value=Mock(returncode=1)):
            self.assertFalse(prune.safe(dep, {str(dep.parent / 'server.js')}, set(), time.time()))
            self.assertFalse(prune.safe(dep, {str(dep.parent)}, set(), time.time()))

    def test_vanished_docker_object_keeps_known_snapshot(self):
        # intent: a completed CI container cannot abort root reclamation during inspect.
        response = Mock(returncode=1, stdout='[{"Id":"remaining"}]', stderr='Error: No such object: gone')
        with patch.object(prune.subprocess, 'run', return_value=response):
            self.assertEqual(prune.inspect_objects(['remaining', 'gone']), [{'Id': 'remaining'}])

    def test_docker_inspection_unknown_fails_closed(self):
        # intent: unknown mount ownership never authorizes filesystem deletion.
        response = Mock(returncode=1, stdout='[]', stderr='permission denied')
        with patch.object(prune.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'DOCKER_INSPECTION_FAILED'):
                prune.inspect_objects(['unknown'])

    def test_package_credentials_module_is_not_secret_material(self):
        # intent: a source module named credentials.py does not make an old venv permanent.
        dep = self.root / '.venv'
        dep.mkdir()
        module = dep / 'credentials.py'
        module.write_text('# library source')
        for p in (dep, module):
            os.utime(p, (time.time() - 90000,) * 2)
        original = subprocess.run
        def execute(args, **kwargs):
            if args[0] == 'fuser':
                return Mock(returncode=1)
            return original(args, **kwargs)
        with patch.object(prune.subprocess, 'run', side_effect=execute):
            self.assertTrue(prune.safe(dep, set(), set(), time.time()))
        db = dep / 'PG_VERSION'
        db.touch()
        os.utime(db, (time.time() - 90000,) * 2)
        os.utime(dep, (time.time() - 90000,) * 2)
        with patch.object(prune.subprocess, 'run', side_effect=execute):
            self.assertFalse(prune.safe(dep, set(), set(), time.time()))

    def test_vanished_candidate_is_retained(self):
        # intent: concurrent cleanup of one candidate cannot abort later tiers.
        self.old.unlink()
        self.assertFalse(prune.safe(self.old, set(), set(), time.time()))

    def test_threshold_boundary(self):
        # intent: reclamation starts at 75 percent and does nothing at 74.9 percent.
        with patch.object(prune, 'root_state', return_value=(74.9, 100)), patch.object(prune, 'activity') as scan:
            prune.run(False)
            scan.assert_not_called()
        with patch.object(prune, 'root_state', return_value=(75, 100)), patch.object(prune, 'activity', side_effect=RuntimeError('boundary')):
            with self.assertRaisesRegex(RuntimeError, 'boundary'):
                prune.run(False)


if __name__ == '__main__':
    unittest.main()
