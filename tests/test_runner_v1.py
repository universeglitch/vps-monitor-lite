import unittest

from auth import Busy
from function import BoundedCommand


class Gate:
    def checkpoint(self): pass


class RunnerTests(unittest.TestCase):
    def test_isolated_requires_injected_launcher(self):
        with self.assertRaises(Busy):
            BoundedCommand(Gate()).run(['/bin/echo','hi'])

    def test_untrusted_argv_rejected(self):
        with self.assertRaises(Exception):
            BoundedCommand(Gate(),lambda argv:(argv,lambda:None)).run('echo hi')

    def test_timeout_cancels_transient_unit(self):
        # The transient cgroup is owned by systemd, not by the systemd-run client
        # process group: killing the client cannot be treated as the task stopping.
        # The injected launcher must return an explicit canceller that stops the
        # unique unit, and it must be invoked on the timeout path.
        cancelled=[]
        def launcher(argv):
            return ['/bin/sleep','10'], lambda: cancelled.append(argv)
        runner=BoundedCommand(Gate(),launcher)
        with self.assertRaises(Busy):
            runner.run(['/bin/echo','ignored'],timeout=0.3)
        self.assertEqual(len(cancelled),1)

    def test_success_still_cancels_transient_unit(self):
        cancelled=[]
        def launcher(argv):
            return ['/bin/echo','ok'], lambda: cancelled.append(True)
        runner=BoundedCommand(Gate(),launcher)
        self.assertEqual(runner.run(['/bin/echo','ignored']),b'ok\n')
        self.assertEqual(cancelled,[True])


if __name__=='__main__': unittest.main()
