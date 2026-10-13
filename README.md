# the_thumbdrive_deborker

![Which side are you on? balenaEtcher vs Rufus](img/rufus.jpeg)

Have you ever chosen the wrong tool while trying to create a bootable USB
drive and now your drive is borked? We know we have.

Let me introduce you to the Thumbdrive Deborker. The Thumbdrive Deborker
does just what it says it does: it deborks borkered thumbdrives.

It works by zeroing out the first 10 MB of the drive. That sounds crude, and
it is, but it's the whole trick. Imaging tools like balenaEtcher (the one
that got us) and Rufus leave a stick as a "superfloppy": no partition table at all, just a raw
filesystem (or a half-written ISO9660 header) starting at sector 0, sometimes
with a stale GPT behind it. Windows looks at that, decides the drive is
"RAW" or "Unknown", and Disk Management, `format`, and even `diskpart clean`
refuse to touch it. Mac will usually offer to "initialize" it and then fail.

Writing zeros over the first 10 MB wipes the superfloppy boot sector, any
leftover MBR, the GPT header and its partition entries, and whatever
filesystem metadata was squatting at offset 0. With nothing left to confuse
it, Windows finally sees a blank disk and is willing to put a fresh GPT
partition table on it. From there it's a normal partition plus a FAT32 or
exFAT format, and the stick mounts immediately in File Explorer and Finder.

On Linux that's one line:

```
sudo dd if=/dev/zero of=/dev/sdX bs=1M count=10 status=progress conv=fsync
```

On Windows it's harder, because Windows refuses raw writes to any part of a
disk that has a mounted volume on it. `deborker.py` handles that for you.

## What `deborker.py` does

1. Lists USB disks via `Get-Disk`.
2. Locks and dismounts every volume on the chosen disk (Windows, unlike
   Linux, blocks raw writes under a mounted volume).
3. Zeroes the first 10 MB of `\\.\PhysicalDriveN`.
4. Refreshes the storage cache, clears the disk if it still looks
   initialized, and initializes it as GPT.
5. Creates one max-size partition, stamps the Basic Data GUID so Windows
   never reports it as "Unknown", and formats it.
   - FAT32 for drives up to 32 GB.
   - exFAT above that, because every built-in Windows formatter refuses
     FAT32 over 32 GB. exFAT also mounts on macOS.

## Usage

### Requirements

- Windows 10 or 11.
- Python 3.8 or newer. Get it from [python.org](https://www.python.org/downloads/)
  or the Microsoft Store. No extra packages are needed; the script only uses
  the standard library plus PowerShell, which Windows already has.
- An **Administrator** terminal. Raw disk access is refused otherwise.
- The borked drive plugged directly into the Windows machine. If you have
  attached it to WSL with usbipd, detach it first (see step 2).

### Step by step

1. **Get the script.** Either clone the repo:

   ```
   git clone https://github.com/foodbark/the_thumbdrive_deborker.git
   cd the_thumbdrive_deborker
   ```

   or just download `deborker.py` from GitHub and put it somewhere handy.

2. **Make sure Windows owns the drive.** If you have ever run
   `usbipd attach` on it, give it back to Windows from an elevated PowerShell:

   ```powershell
   usbipd list                       # find the BUSID of the stick
   usbipd detach --busid <BUSID>
   usbipd unbind --busid <BUSID>
   ```

   Skip this if you have never used usbipd.

3. **Open an Administrator terminal.** Right-click Windows Terminal,
   PowerShell, or Command Prompt and choose *Run as administrator*. A
   regular terminal will print an error and exit.

4. **Run the script.**

   ```
   python deborker.py
   ```

5. **Pick the disk.** The script lists every USB disk Windows can see and
   asks for a disk number. Match it by name and size. It will refuse any
   number that is not in its USB list, so you cannot accidentally pick your
   system drive, but still look twice.

6. **Enter a label** (or press Enter for `PNYUNBORKED`). Up to 11 letters,
   digits, spaces, dashes or underscores; anything else is stripped.

7. **Type `YES`** (all caps) at the warning. Anything else aborts with
   nothing touched. After `YES` the drive is wiped, re-partitioned, and
   formatted. The drive letter is printed at the end and File Explorer
   usually opens it on its own.

### What a run looks like

```
=== The Thumb Drive De-Borker ===

Detected USB Drives:
  [2] PNY USB 3.0 FD (57.74 GB) - Style: RAW

Enter Disk Number to de-bork: 2
Enter volume label (max 11 chars, default: PNYUNBORKED):

[!] WARNING: Entire contents of Disk 2 (PNY USB 3.0 FD) will be destroyed.
    It will be re-created as GPT + exFAT labeled 'PNYUNBORKED'.
Type 'YES' to proceed: YES
[*] Locked and dismounted \\.\E:
[*] Opening raw device \\.\PhysicalDrive2...
[*] Wiped 10MB / 10MB...
[+] First 10MB zeroed; old partition headers are gone.
[*] Reinitializing as GPT...
[*] Partitioning and formatting as exFAT labeled 'PNYUNBORKED'...
E: PNYUNBORKED exFAT 57.72 GB

[+] Drive successfully de-borked and mounted.
```

**Warning:** this destroys everything on the selected disk. There is no
undo. Double-check the disk number before typing `YES`.

### Troubleshooting

- **"This script requires administrator privileges"**: the terminal is not
  elevated. Close it and reopen with *Run as administrator*.
- **"No USB disks detected by Windows"**: the stick is not plugged in, is
  still attached to WSL via usbipd (step 2), or is so dead that Windows
  does not enumerate it at all. Check Device Manager under *Disk drives*.
- **"Windows refused the raw write"**: something still has a volume on the
  disk open. Close File Explorer windows pointing at it, stop any antivirus
  scan, unplug and replug the stick, and run again.
- **You wanted FAT32 but got exFAT**: the drive is over 32 GB and Windows
  will not format FAT32 that large. Use the manual WSL recipe below, where
  `mkfs.vfat` has no such limit.
- **Mac still shows it as unreadable**: unplug, replug. If it persists, the
  format did not complete; check the last lines of the script output for a
  PowerShell error.

## Tests

The Windows-only parts (PowerShell, kernel32, raw device writes) are mocked,
so the tests run anywhere:

```
python -m unittest -v test_deborker
```

## Manual WSL recipe (the original fix)

If you want FAT32 on a drive bigger than 32 GB, or just prefer Linux tools,
this is the sequence the script automates. In an elevated PowerShell:

```powershell
usbipd list                         # find the BUSID, e.g. 2-5
usbipd bind --busid <BUSID> --force
usbipd attach --wsl --busid <BUSID>
```

In WSL (confirm the device with `lsblk -o NAME,SIZE,TYPE,TRAN,MOUNTPOINTS`
and make sure MOUNTPOINTS is blank):

```bash
sudo dd if=/dev/zero of=/dev/sdX bs=1M count=10 status=progress conv=fsync
sudo parted -s /dev/sdX mklabel gpt
sudo parted -s /dev/sdX mkpart primary 1MiB 100%
sudo mkfs.vfat -F 32 -n "PNYUNBORKED" /dev/sdX1
```

Back in PowerShell, hand it back to Windows and fix the partition type GUID
(`parted` tags it as a Linux partition, which Windows shows as "Unknown"):

```powershell
usbipd detach --busid <BUSID>
usbipd unbind --busid <BUSID>
Get-Disk | Where-Object BusType -eq 'USB' | Get-Partition -PartitionNumber 1 |
    Set-Partition -GptType '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}'
```

## Credits

- [foodbark](https://github.com/foodbark): deborker of the drives.
- [yusefmarra](https://github.com/yusefmarra): borker of the drives. Every
  project needs a problem, and he supplied it.
- [balenaEtcher](https://etcher.balena.io/): the weapon.

## License

MIT. See [LICENSE](LICENSE).
