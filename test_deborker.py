"""Unit tests for deborker.py. Windows-specific pieces (PowerShell, kernel32,
raw device writes) are mocked so the flow can be exercised anywhere.

Run:  python -m unittest -v test_deborker
"""

import io
import json
import subprocess
import unittest
from unittest import mock

import deborker


def ps_result(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeKernel32:
    """Records CreateFileW / DeviceIoControl / CloseHandle calls."""

    def __init__(self, fail_open=(), fail_lock=()):
        self.fail_open = set(fail_open)
        self.fail_lock = set(fail_lock)
        self.opened = []
        self.ioctls = []
        self.closed = []
        self._next = 100

    def CreateFileW(self, path, *_):
        self.opened.append(path)
        if path in self.fail_open:
            return deborker.INVALID_HANDLE_VALUE
        self._next += 1
        return self._next

    def DeviceIoControl(self, handle, code, *_):
        self.ioctls.append((handle, code))
        path = self.opened[handle - 101]
        if code == deborker.FSCTL_LOCK_VOLUME and path in self.fail_lock:
            return 0
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def GetLastError(self):
        return 5


class FakeRawDisk(io.RawIOBase):
    """Stand-in for open(r'\\.\PhysicalDriveN', 'r+b', buffering=0)."""

    def __init__(self, fail_write=False):
        self.data = bytearray()
        self.fail_write = fail_write
        self.flushed = False

    def writable(self):
        return True

    def write(self, b):
        if self.fail_write:
            raise PermissionError(13, "Access is denied")
        self.data.extend(b)
        return len(b)

    def flush(self):
        self.flushed = True


class EnumerationTests(unittest.TestCase):
    @mock.patch("deborker.subprocess.run")
    def test_single_disk_dict_becomes_list(self, run):
        run.return_value = ps_result(json.dumps({"Number": 1, "FriendlyName": "PNY", "Size": 1, "PartitionStyle": "RAW"}))
        self.assertEqual(deborker.get_usb_disks()[0]["Number"], 1)
        self.assertEqual(len(deborker.get_usb_disks()), 1)

    @mock.patch("deborker.subprocess.run")
    def test_multiple_disks_list(self, run):
        run.return_value = ps_result(json.dumps([{"Number": 1}, {"Number": 2}]))
        self.assertEqual([d["Number"] for d in deborker.get_usb_disks()], [1, 2])

    @mock.patch("deborker.subprocess.run")
    def test_no_disks_empty_output(self, run):
        run.return_value = ps_result("")
        self.assertEqual(deborker.get_usb_disks(), [])

    @mock.patch("deborker.subprocess.run")
    def test_ps_failure_raises(self, run):
        run.return_value = ps_result("", 1, "boom")
        with self.assertRaises(RuntimeError) as cm:
            deborker.get_usb_disks()
        self.assertIn("boom", str(cm.exception))

    @mock.patch("deborker.subprocess.run")
    def test_ps_invoked_without_profile(self, run):
        run.return_value = ps_result("")
        deborker.run_ps("Get-Date")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "powershell")
        self.assertIn("-NoProfile", argv)
        self.assertEqual(argv[-1], "Get-Date")


class VolumePathTests(unittest.TestCase):
    @mock.patch("deborker.subprocess.run")
    def test_drive_letter_preferred_over_guid_path(self, run):
        run.return_value = ps_result(json.dumps([
            {"DriveLetter": "E", "Path": "\\\\?\\Volume{aaa}\\"},
            {"DriveLetter": None, "Path": "\\\\?\\Volume{bbb}\\"},
            {"DriveLetter": "", "Path": "\\\\?\\Volume{ccc}\\"},
        ]))
        self.assertEqual(
            deborker.get_volume_device_paths(3),
            [r"\\.\E:", r"\\?\Volume{bbb}", r"\\?\Volume{ccc}"],
        )
        self.assertIn("Get-Partition -DiskNumber 3", run.call_args.args[0][-1])

    @mock.patch("deborker.subprocess.run")
    def test_no_volumes(self, run):
        run.return_value = ps_result("")
        self.assertEqual(deborker.get_volume_device_paths(3), [])

    @mock.patch("deborker.subprocess.run")
    def test_ps_error_is_swallowed(self, run):
        run.return_value = ps_result("", 1, "nope")
        self.assertEqual(deborker.get_volume_device_paths(3), [])


class LockDismountTests(unittest.TestCase):
    def test_lock_then_dismount_each_volume_and_keep_handles(self):
        k = FakeKernel32()
        with mock.patch("deborker._kernel32", return_value=k), mock.patch("sys.stdout", new_callable=io.StringIO):
            handles = deborker.lock_and_dismount_volumes([r"\\.\E:", r"\\?\Volume{x}"])
        self.assertEqual(len(handles), 2)
        self.assertEqual(
            [c for _, c in k.ioctls],
            [deborker.FSCTL_LOCK_VOLUME, deborker.FSCTL_DISMOUNT_VOLUME] * 2,
        )
        self.assertEqual(k.closed, [])  # caller closes later

    def test_unopenable_volume_is_skipped(self):
        k = FakeKernel32(fail_open=[r"\\.\E:"])
        with mock.patch("deborker._kernel32", return_value=k), mock.patch("sys.stdout", new_callable=io.StringIO):
            handles = deborker.lock_and_dismount_volumes([r"\\.\E:", r"\\.\F:"])
        self.assertEqual(len(handles), 1)
        self.assertEqual(len(k.ioctls), 2)

    def test_lock_failure_still_dismounts(self):
        k = FakeKernel32(fail_lock=[r"\\.\E:"])
        with mock.patch("deborker._kernel32", return_value=k), mock.patch("sys.stdout", new_callable=io.StringIO):
            handles = deborker.lock_and_dismount_volumes([r"\\.\E:"])
        self.assertEqual(len(handles), 1)
        self.assertEqual([c for _, c in k.ioctls], [deborker.FSCTL_LOCK_VOLUME, deborker.FSCTL_DISMOUNT_VOLUME])


class ZeroHeadTests(unittest.TestCase):
    def test_writes_exactly_10mb_of_zeros_in_1mb_chunks(self):
        disk = FakeRawDisk()
        k = FakeKernel32()
        opened = {}

        def fake_open(path, mode, buffering=-1):
            opened["path"], opened["mode"], opened["buffering"] = path, mode, buffering
            return disk

        with mock.patch("deborker._kernel32", return_value=k), \
             mock.patch("deborker.get_volume_device_paths", return_value=[r"\\.\E:"]), \
             mock.patch("builtins.open", side_effect=fake_open), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            deborker.zero_disk_head(4)

        self.assertEqual(opened, {"path": r"\\.\PhysicalDrive4", "mode": "r+b", "buffering": 0})
        self.assertEqual(len(disk.data), 10 * 1024 * 1024)
        self.assertEqual(disk.data.count(0), len(disk.data))
        self.assertTrue(disk.flushed)
        self.assertEqual(k.closed, [101])  # lock released after the write

    def test_odd_byte_count_is_honoured(self):
        disk = FakeRawDisk()
        with mock.patch("deborker._kernel32", return_value=FakeKernel32()), \
             mock.patch("deborker.get_volume_device_paths", return_value=[]), \
             mock.patch("builtins.open", return_value=disk), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            deborker.zero_disk_head(4, num_bytes=1024 * 1024 + 512)
        self.assertEqual(len(disk.data), 1024 * 1024 + 512)

    def test_permission_error_becomes_runtime_error_and_releases_locks(self):
        k = FakeKernel32()
        with mock.patch("deborker._kernel32", return_value=k), \
             mock.patch("deborker.get_volume_device_paths", return_value=[r"\\.\E:"]), \
             mock.patch("builtins.open", return_value=FakeRawDisk(fail_write=True)), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(RuntimeError) as cm:
                deborker.zero_disk_head(4)
        self.assertIn("refused the raw write", str(cm.exception))
        self.assertEqual(k.closed, [101])


class PowerShellCommandTests(unittest.TestCase):
    @mock.patch("deborker.subprocess.run", return_value=ps_result(""))
    def test_reinit_refreshes_cache_and_only_clears_initialized_disks(self, run):
        deborker.reinit_disk_gpt(5)
        cmd = run.call_args.args[0][-1]
        self.assertLess(cmd.index("Update-HostStorageCache"), cmd.index("Clear-Disk"))
        self.assertIn("if ($d.PartitionStyle -ne 'RAW') { Clear-Disk -Number 5 -RemoveData -RemoveOEM -Confirm:$false }", cmd)
        self.assertIn("Initialize-Disk -Number 5 -PartitionStyle GPT", cmd)
        self.assertLess(cmd.index("Clear-Disk"), cmd.index("Initialize-Disk"))
        self.assertIn("Set-Disk -Number 5 -IsReadOnly $false", cmd)
        self.assertIn("Set-Disk -Number 5 -IsOffline $false", cmd)

    @mock.patch("deborker.subprocess.run", return_value=ps_result("E: PNYUNBORKED FAT32 28.9 GB"))
    def test_partition_and_format_command(self, run):
        out = deborker.partition_and_format(5, "FAT32", "PNYUNBORKED")
        cmd = run.call_args.args[0][-1]
        self.assertIn("New-Partition -DiskNumber 5 -UseMaximumSize -AssignDriveLetter", cmd)
        self.assertIn("Set-Partition -GptType '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}'", cmd)
        self.assertIn("Format-Volume -FileSystem FAT32 -NewFileSystemLabel 'PNYUNBORKED' -Force -Confirm:$false", cmd)
        self.assertEqual(out, "E: PNYUNBORKED FAT32 28.9 GB")


class ChoiceTests(unittest.TestCase):
    def test_filesystem_by_size(self):
        gib = 1024 ** 3
        self.assertEqual(deborker.choose_filesystem(16 * gib), "FAT32")
        self.assertEqual(deborker.choose_filesystem(32 * gib), "FAT32")
        self.assertEqual(deborker.choose_filesystem(32 * gib + 1), "exFAT")
        self.assertEqual(deborker.choose_filesystem(62_000_000_000), "exFAT")  # a "58 GB" stick

    def test_label_sanitising(self):
        self.assertEqual(deborker.sanitize_label(""), "PNYUNBORKED")
        self.assertEqual(deborker.sanitize_label("   "), "PNYUNBORKED")
        self.assertEqual(deborker.sanitize_label("pnyunborked2"), "PNYUNBORKED")  # truncated to 11
        self.assertEqual(deborker.sanitize_label("my stick"), "MY STICK")
        self.assertEqual(deborker.sanitize_label("it's'; rm"), "ITS RM")  # no PS-quote injection


class MainFlowTests(unittest.TestCase):
    """Drive main() end-to-end with every Windows touchpoint mocked."""

    DISKS = [
        {"Number": 2, "FriendlyName": "PNY USB 3.0", "SerialNumber": "X", "Size": 62_000_000_000, "PartitionStyle": "RAW"},
        {"Number": 3, "FriendlyName": "Small Stick", "SerialNumber": "Y", "Size": 8 * 1024 ** 3, "PartitionStyle": "MBR"},
    ]

    def run_main(self, inputs, admin=True, disks=None, raw_disk=None):
        disks = self.DISKS if disks is None else disks
        ps_calls = []

        def fake_run(argv, **kw):
            cmd = argv[-1]
            ps_calls.append(cmd)
            if "Get-Disk | Where-Object BusType" in cmd:
                return ps_result(json.dumps(disks) if disks else "")
            if "Get-Partition -DiskNumber" in cmd:
                return ps_result(json.dumps({"DriveLetter": "E", "Path": "\\\\?\\Volume{z}\\"}))
            if "Format-Volume" in cmd:
                return ps_result("E: LABEL FS 1 GB")
            return ps_result("")

        raw_disk = raw_disk or FakeRawDisk()
        k = FakeKernel32()
        out = io.StringIO()
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return inputs.pop(0)

        with mock.patch("deborker.is_admin", return_value=admin), \
             mock.patch("deborker.subprocess.run", side_effect=fake_run), \
             mock.patch("deborker._kernel32", return_value=k), \
             mock.patch("builtins.open", return_value=raw_disk), \
             mock.patch("builtins.input", side_effect=fake_input), \
             mock.patch("sys.stdout", out):
            try:
                deborker.main()
                code = 0
            except SystemExit as e:
                code = e.code
        self.prompts = prompts
        return code, out.getvalue(), ps_calls, raw_disk, k

    def test_requires_admin(self):
        code, out, ps, *_ = self.run_main([], admin=False)
        self.assertEqual(code, 1)
        self.assertIn("administrator", out.lower())
        self.assertEqual(ps, [])

    def test_no_usb_disks(self):
        code, out, *_ = self.run_main([], disks=[])
        self.assertEqual(code, 1)
        self.assertIn("No USB disks", out)

    def test_non_numeric_disk(self):
        code, out, *_ = self.run_main(["abc"])
        self.assertEqual(code, 1)
        self.assertIn("Invalid input", out)

    def test_disk_not_in_usb_list_refused(self):
        code, out, ps, raw, _ = self.run_main(["0"])  # 0 is the system NVMe, not listed
        self.assertEqual(code, 1)
        self.assertIn("not a valid USB target", out)
        self.assertEqual(len(raw.data), 0)

    def test_confirmation_required(self):
        for answer in ["yes", "y", "", "YES "]:
            with self.subTest(answer=repr(answer)):
                code, out, ps, raw, _ = self.run_main(["2", "", answer])
                self.assertIn("will be destroyed", self.prompts[-1])
                if answer.strip() == "YES":
                    self.assertEqual(code, 0)
                else:
                    self.assertEqual(code, 0)
                    self.assertIn("Aborted", out)
                    self.assertEqual(len(raw.data), 0)
                    self.assertFalse(any("Clear-Disk" in c or "Format-Volume" in c for c in ps))

    def test_happy_path_big_stick_uses_exfat_and_correct_order(self):
        code, out, ps, raw, k = self.run_main(["2", "pnyunborked2", "YES"])
        self.assertEqual(code, 0)
        self.assertIn("GPT + exFAT labeled 'PNYUNBORKED'", self.prompts[-1])  # shown before YES
        self.assertEqual(len(raw.data), 10 * 1024 * 1024)
        # volume on disk 2 was locked/dismounted before the write, then released
        self.assertEqual(k.opened, [r"\\.\E:"])
        self.assertEqual(k.closed, [101])
        # PS order: enumerate, find volumes, reinit, partition+format
        kinds = []
        for c in ps:
            if "Get-Disk | Where-Object" in c: kinds.append("enum")
            elif "Get-Partition -DiskNumber 2" in c: kinds.append("vols")
            elif "Initialize-Disk -Number 2" in c: kinds.append("reinit")
            elif "Format-Volume -FileSystem exFAT -NewFileSystemLabel 'PNYUNBORKED'" in c: kinds.append("format")
            else: kinds.append("other:" + c[:40])
        self.assertEqual(kinds, ["enum", "vols", "reinit", "format"])
        self.assertIn("successfully de-borked", out)

    def test_happy_path_small_stick_uses_fat32(self):
        code, out, ps, *_ = self.run_main(["3", "", "YES"])
        self.assertEqual(code, 0)
        self.assertTrue(any("Format-Volume -FileSystem FAT32 -NewFileSystemLabel 'PNYUNBORKED'" in c for c in ps))
        self.assertTrue(any("Initialize-Disk -Number 3" in c for c in ps))

    def test_raw_write_refused_exits_cleanly_without_formatting(self):
        code, out, ps, *_ = self.run_main(["2", "", "YES"], raw_disk=FakeRawDisk(fail_write=True))
        self.assertEqual(code, 1)
        self.assertIn("refused the raw write", out)
        self.assertFalse(any("Format-Volume" in c for c in ps))
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
