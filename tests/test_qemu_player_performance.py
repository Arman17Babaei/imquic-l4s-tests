"""Host performance profile lifecycle; no QEMU or host settings are changed."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch,call
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/l4s'))
import qemu_player_performance as profile


class PerformanceTests(unittest.TestCase):
    def test_power_mode_restored_after_success_or_launcher_failure(self):
        for error in (None,RuntimeError('boot failed')):
            with self.subTest(error=error), patch.object(profile.subprocess,'check_output',return_value='balanced\n'), \
                 patch.object(profile.subprocess,'run') as run, \
                 patch.object(profile.runner,'main',side_effect=error), patch.object(profile.runner,'boot_qemu'):
                if error:
                    with self.assertRaisesRegex(RuntimeError,'boot failed'):profile.main()
                else:profile.main()
                self.assertEqual(run.call_args_list,[call(['powerprofilesctl','set','performance'],check=True),
                    call(['powerprofilesctl','set','balanced'],check=True)])
