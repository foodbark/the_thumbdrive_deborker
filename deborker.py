import sys
import subprocess
import json
import ctypes

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def run_ps(cmd):
    """Run a PowerShell command and return stdout string."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        capture_output=True,
        text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PowerShell error:\n{proc.stderr.strip()}")
    return proc.stdout.strip()

def get_usb_disks():
    """Enumerate USB disks using Get-Disk serialized as JSON."""
    ps_cmd = (
        "Get-Disk | Where-Object BusType -eq 'USB' | "
        "Select-Object Number, FriendlyName, SerialNumber, Size, PartitionStyle | "
        "ConvertTo-Json -Compress"
    )
    output = run_ps(ps_cmd)
    if not output:
        return []
    data = json.loads(output)
    if isinstance(data, dict):
        return [data]
    return data

def zero_disk_head(disk_number, num_bytes=10 * 1024 * 1024):
    """Open raw PhysicalDrive handle on Windows and write null bytes to kill sector 0."""
    device_path = rf"\\.\PhysicalDrive{disk_number}"
    print(f"[*] Opening raw device {device_path}...")
    
    # 1MB buffer of zeros
    chunk_size = 1024 * 1024
    zero_chunk = b"\x00" * chunk_size
    bytes_written = 0

    # Must open in binary write mode
    with open(device_path, "r+b", buffering=0) as disk:
        while bytes_written < num_bytes:
            write_len = min(chunk_size, num_bytes - bytes_written)
            disk.write(zero_chunk[:write_len])
            bytes_written += write_len
            print(f"\r[*] Wiped {bytes_written // (1024 * 1024)}MB / {num_bytes // (1024 * 1024)}MB...", end="")
    print("\n[+] Sector 0 and partition table headers zeroed.")

def main():
    if not is_admin():
        print("[-] Error: This script requires administrator privileges to access raw disks.")
        print("    Please run terminal as Administrator.")
        sys.exit(1)

    print("=== The Thumb Drive De-Borker ===")
    disks = get_usb_disks()

    if not disks:
        print("[-] No USB disks detected by Windows. Ensure the drive is plugged in and unbound from usbipd.")
        sys.exit(1)

    print("\nDetected USB Drives:")
    for d in disks:
        size_gb = round(d['Size'] / (1024**3), 2)
        print(f"  [{d['Number']}] {d['FriendlyName']} ({size_gb} GB) - Style: {d['PartitionStyle']}")

    disk_input = input("\nEnter Disk Number to de-bork: ").strip()
    try:
        disk_num = int(disk_input)
    except ValueError:
        print("[-] Invalid input.")
        sys.exit(1)

    selected = next((d for d in disks if d['Number'] == disk_num), None)
    if not selected:
        print(f"[-] Disk {disk_num} is not a valid USB target.")
        sys.exit(1)

    label = input("Enter volume label (max 11 chars, default: PNYUNBORKED): ").strip()
    if not label:
        label = "PNYUNBORKED"
    label = label[:11].upper()

    confirm = input(f"\n[!] WARNING: Entire contents of Disk {disk_num} ({selected['FriendlyName']}) will be destroyed.\nType 'YES' to proceed: ").strip()
    if confirm != "YES":
        print("[-] Aborted.")
        sys.exit(0)

    # 1. Zero out raw LBA 0 to kill superfloppy header
    zero_disk_head(disk_num)

    # 2. Reset partition table via PowerShell
    print("[*] Reinitializing as GPT...")
    run_ps(f"Clear-Disk -Number {disk_num} -RemoveData -RemoveOEM -Confirm:$false; Initialize-Disk -Number {disk_num} -PartitionStyle GPT")

    # 3. Create partition, assign GPT Basic Data GUID, and format FAT32
    print(f"[*] Partitioning and formatting as FAT32 labeled '{label}'...")
    ps_format = (
        f"$part = New-Partition -DiskNumber {disk_num} -UseMaximumSize -AssignDriveLetter; "
        f"$part | Set-Partition -GptType '{{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}}'; "
        f"$part | Format-Volume -FileSystem FAT32 -NewFileSystemLabel '{label}' -Force -Confirm:$false"
    )
    output = run_ps(ps_format)
    print(output)
    print("\n[+] Drive successfully de-borked and mounted.")

if __name__ == "__main__":
    main()
