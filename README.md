# the_thumbdrive_deborker

![Which side are you on? balenaEtcher vs Rufus](img/rufus.jpeg)

Have you ever chosen the wrong tool while trying to create a bootable USB
drive and now your drive is borked? We know we have.

Let me introduce you to the Thumbdrive Deborker. The Thumbdrive Deborker
does just what it says it does: it deborks borkered thumbdrives.

It works by zeroing out the first 10 MB of the drive. That sounds crude, and
it is, but it's the whole trick. Imaging tools like Etcher and Rufus often
leave a stick as a "superfloppy": no partition table at all, just a raw
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

Run from an **Administrator** terminal on Windows:

```
python deborker.py
```

Pick the disk number, enter a label (11 chars max), and type `YES` to confirm.

**Warning:** this destroys everything on the selected disk. Double-check the
disk number before confirming.

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
