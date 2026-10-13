"""The Thumb Drive De-Borker.

Fixes USB drives that Windows refuses to format (superfloppy layouts, dead
partition headers, drives left weird after usbipd). The core trick is the
same as `dd if=/dev/zero of=/dev/sdX bs=1M count=10` on Linux: zero the
first 10 MB so the old headers are gone, then build a fresh GPT + FAT32/exFAT
layout that mounts on both Windows and macOS.

Windows, unlike Linux, blocks raw writes that land inside a mounted volume,
so before wiping we lock and dismount every volume on the target disk the
same way Rufus / dd-for-Windows do.

Must be run from an Administrator terminal on Windows.
"""

import ctypes
import json
import subprocess
import sys

HEAD_WIPE_BYTES = 10 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
FAT32_MAX_BYTES = 32 * 1024 ** 3  # Windows refuses FAT32 above this
DEFAULT_LABEL = "PNYUNBORKED"
MAX_LABEL_LEN = 11  # FAT32 and exFAT volume label limit
BASIC_DATA_GUID = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"  # so Windows never shows "Unknown"

# Win32 constants
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020


def _kernel32():
    return ctypes.windll.kernel32


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_ps(cmd):
    """Run a PowerShell command and return stdout string."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PowerShell error:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def _ps_json(cmd):
    """Run a PowerShell command that ends in ConvertTo-Json and always return a list."""
    output = run_ps(cmd)
    if not output:
        return []
    data = json.loads(output)
    if isinstance(data, dict):
        return [data]
    return data


def get_usb_disks():
    """Enumerate USB disks using Get-Disk serialized as JSON."""
    return _ps_json(
        "Get-Disk | Where-Object BusType -eq 'USB' | "
        "Select-Object Number, FriendlyName, SerialNumber, Size, PartitionStyle | "
        "ConvertTo-Json -Compress"
    )


def get_volume_device_paths(disk_number):
    r"""Return device paths (\\.\X: or \\?\Volume{guid}) for every volume on the disk."""
    try:
        vols = _ps_json(
            f"Get-Partition -DiskNumber {disk_number} -ErrorAction SilentlyContinue | "
            "Get-Volume -ErrorAction SilentlyContinue | "
            "Select-Object DriveLetter, Path | ConvertTo-Json -Compress"
        )
    except RuntimeError:
        return []
    paths = []
    for v in vols:
        letter = v.get("DriveLetter")
        if letter and str(letter).strip():
            paths.append(rf"\\.\{str(letter).strip()}:")
        elif v.get("Path"):
            paths.append(v["Path"].rstrip("\\"))
    return paths


def lock_and_dismount_volumes(paths):
    """Lock + dismount each volume so Windows allows raw writes under it.

    Returns the open handles; the caller must keep them open while writing
    and close them afterwards (closing releases the lock).
    """
    k32 = _kernel32()
    handles = []
    for path in paths:
        h = k32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if h == INVALID_HANDLE_VALUE:
            print(f"[!] Could not open volume {path} (error {k32.GetLastError()}), continuing.")
            continue
        returned = ctypes.c_ulong(0)
        locked = k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(returned), None)
        if not locked:
            print(f"[!] Could not lock volume {path} (error {k32.GetLastError()}); something may have it open.")
        dismounted = k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(returned), None)
        if not dismounted:
            print(f"[!] Could not dismount volume {path} (error {k32.GetLastError()}).")
        else:
            print(f"[*] Locked and dismounted {path}")
        handles.append(h)
    return handles


def close_handles(handles):
    k32 = _kernel32()
    for h in handles:
        k32.CloseHandle(h)


def zero_disk_head(disk_number, num_bytes=HEAD_WIPE_BYTES):
    r"""Write zeros over the first num_bytes of \\.\PhysicalDriveN (dd equivalent)."""
    device_path = rf"\\.\PhysicalDrive{disk_number}"
    handles = lock_and_dismount_volumes(get_volume_device_paths(disk_number))
    try:
        print(f"[*] Opening raw device {device_path}...")
        zero_chunk = b"\x00" * CHUNK_BYTES
        bytes_written = 0
        try:
            with open(device_path, "r+b", buffering=0) as disk:
                while bytes_written < num_bytes:
                    write_len = min(CHUNK_BYTES, num_bytes - bytes_written)
                    disk.write(zero_chunk[:write_len])
                    bytes_written += write_len
                    print(
                        f"\r[*] Wiped {bytes_written // CHUNK_BYTES}MB / {num_bytes // CHUNK_BYTES}MB...",
                        end="",
                    )
                disk.flush()
        except PermissionError as e:
            raise RuntimeError(
                f"Windows refused the raw write to {device_path} ({e}). "
                "A volume on the disk is still mounted or in use; close any Explorer "
                "windows / files on it and retry."
            ) from e
        print("\n[+] First 10MB zeroed; old partition headers are gone.")
    finally:
        close_handles(handles)


def reinit_disk_gpt(disk_number):
    """Refresh Windows' view of the disk, clear it if needed, and init as GPT."""
    run_ps(
        "Update-HostStorageCache; "
        f"Update-Disk -Number {disk_number} -ErrorAction SilentlyContinue; "
        f"Set-Disk -Number {disk_number} -IsReadOnly $false -ErrorAction SilentlyContinue; "
        f"Set-Disk -Number {disk_number} -IsOffline $false -ErrorAction SilentlyContinue; "
        f"$d = Get-Disk -Number {disk_number}; "
        "if ($d.PartitionStyle -ne 'RAW') { "
        f"Clear-Disk -Number {disk_number} -RemoveData -RemoveOEM -Confirm:$false "
        "}; "
        f"Initialize-Disk -Number {disk_number} -PartitionStyle GPT"
    )


def choose_filesystem(size_bytes):
    """FAT32 is the most portable, but Windows won't format it above 32GB."""
    return "FAT32" if size_bytes <= FAT32_MAX_BYTES else "exFAT"


def sanitize_label(label):
    label = "".join(ch for ch in label.strip() if ch.isalnum() or ch in " _-")
    if not label:
        label = DEFAULT_LABEL
    return label[:MAX_LABEL_LEN].upper()


def partition_and_format(disk_number, filesystem, label):
    """Create one max-size partition, stamp the Basic Data GUID, format it. Returns summary text."""
    return run_ps(
        f"$part = New-Partition -DiskNumber {disk_number} -UseMaximumSize -AssignDriveLetter; "
        f"$part | Set-Partition -GptType '{{{BASIC_DATA_GUID}}}'; "
        "Start-Sleep -Seconds 2; "
        f"$vol = $part | Format-Volume -FileSystem {filesystem} "
        f"-NewFileSystemLabel '{label}' -Force -Confirm:$false; "
        "\"$($vol.DriveLetter): $($vol.FileSystemLabel) $($vol.FileSystem) "
        "$([math]::Round($vol.Size / 1GB, 2)) GB\""
    )


def main():
    if not is_admin():
        print("[-] Error: This script requires administrator privileges to access raw disks.")
        print("    Please run terminal as Administrator.")
        sys.exit(1)

    print("=== The Thumb Drive De-Borker ===")
    try:
        disks = get_usb_disks()
    except RuntimeError as e:
        print(f"[-] {e}")
        sys.exit(1)

    if not disks:
        print("[-] No USB disks detected by Windows. Ensure the drive is plugged in and unbound from usbipd.")
        sys.exit(1)

    print("\nDetected USB Drives:")
    for d in disks:
        size_gb = round(d["Size"] / (1024 ** 3), 2)
        print(f"  [{d['Number']}] {d['FriendlyName']} ({size_gb} GB) - Style: {d['PartitionStyle']}")

    disk_input = input("\nEnter Disk Number to de-bork: ").strip()
    try:
        disk_num = int(disk_input)
    except ValueError:
        print("[-] Invalid input.")
        sys.exit(1)

    selected = next((d for d in disks if d["Number"] == disk_num), None)
    if not selected:
        print(f"[-] Disk {disk_num} is not a valid USB target.")
        sys.exit(1)

    filesystem = choose_filesystem(selected["Size"])
    label = sanitize_label(input(f"Enter volume label (max {MAX_LABEL_LEN} chars, default: {DEFAULT_LABEL}): "))

    confirm = input(
        f"\n[!] WARNING: Entire contents of Disk {disk_num} ({selected['FriendlyName']}) will be destroyed.\n"
        f"    It will be re-created as GPT + {filesystem} labeled '{label}'.\n"
        "Type 'YES' to proceed: "
    ).strip()
    if confirm != "YES":
        print("[-] Aborted.")
        sys.exit(0)

    try:
        # 1. Zero out the first 10MB to kill superfloppy / stale partition headers
        zero_disk_head(disk_num)

        # 2. Reset partition table
        print("[*] Reinitializing as GPT...")
        reinit_disk_gpt(disk_num)

        # 3. Create partition and format
        print(f"[*] Partitioning and formatting as {filesystem} labeled '{label}'...")
        print(partition_and_format(disk_num, filesystem, label))
    except RuntimeError as e:
        print(f"\n[-] {e}")
        sys.exit(1)

    print("\n[+] Drive successfully de-borked and mounted.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[-] Aborted.")
        sys.exit(130)
