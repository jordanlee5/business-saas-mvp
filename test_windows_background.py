import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.windows_background import prepare_stdio


class WindowsBackgroundTests(unittest.TestCase):
    def test_console_streams_unchanged(self):
        streams = (sys.stdin, sys.stdout, sys.stderr)
        prepare_stdio()
        self.assertEqual(streams, (sys.stdin, sys.stdout, sys.stderr))

    def test_missing_streams_usable(self):
        with patch.object(sys, 'stdin', None), patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
            prepare_stdio()
            streams = (sys.stdin, sys.stdout, sys.stderr)
            try:
                self.assertEqual(sys.stdin.read(), '')
                print('virtual output')
                print('virtual error', file=sys.stderr)
                prepare_stdio()
                self.assertEqual(streams, (sys.stdin, sys.stdout, sys.stderr))
            finally:
                for stream in streams:
                    stream.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows console handles and pythonw')
    def test_native_windowless_process_and_exit_codes(self):
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        self.assertTrue(pythonw.is_file())
        repo = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='quiet-task-') as folder:
            output = Path(folder) / 'result.json'
            script = Path(folder) / 'probe.py'
            # Only synthetic callbacks: never contacts a server or reads private config.
            script.write_text('''import ctypes,json,sys,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
output,kind,scenario=sys.argv[2:]
from tools import staging_offsite_backup as off
from tools import staging_external_observer as obs
module=off if kind=='pull' else obs
sys.argv=['probe','pull','--config','virtual.json'] if kind=='pull' else ['probe','--config','virtual.json']
kwargs={'return_value':{'status':scenario}}
if scenario=='busy':kwargs={'side_effect':off.BusyError()}
if scenario=='error':kwargs={'side_effect':ValueError('virtual error')}
with patch.object(module,'pull' if kind=='pull' else 'observe',**kwargs):
    result=module.main()
child=subprocess.run([str(Path(sys.executable).with_name('python.exe')),'-S','-c',
    'import ctypes;print(ctypes.windll.kernel32.GetConsoleWindow())'],
    capture_output=True,text=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
Path(output).write_text(json.dumps({'parent_console':ctypes.windll.kernel32.GetConsoleWindow(),
    'child_console':int(child.stdout),'result':result,'stdio_present':all(x is not None for x in (sys.stdin,sys.stdout,sys.stderr))}))
raise SystemExit(result)
''', encoding='utf-8')
            for kind, scenario, expected in [('pull','ok',0),('pull','alert',1),('pull','busy',75),('pull','error',2),
                                               ('observe','ok',0),('observe','alert',1),('observe','pending',0),('observe','busy',0),('observe','error',1)]:
                with self.subTest(kind=kind, scenario=scenario):
                    process = subprocess.run([str(pythonw), '-S', str(script), str(repo), str(output), kind, scenario],
                                             timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
                    report = json.loads(output.read_text())
                    self.assertEqual(process.returncode, expected)
                    self.assertEqual(report, dict(parent_console=0,child_console=0,result=expected,stdio_present=True))


if __name__ == '__main__':
    unittest.main()
