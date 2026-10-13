# drive_de_borker

A small Windows script that un-borks thumb drives that Windows refuses to
format (e.g. superfloppy drives with a dead sector 0, or drives left in a
weird state after being bound to usbipd).

## What it does

1. Lists USB disks via `Get-Disk`.
2. Zeroes the first 10 MB of the selected disk to kill the old partition
   headers.
3. Re-initializes the disk as GPT, creates one max-size partition, and
   formats it FAT32 with the label you choose.

## Usage

Run from an **Administrator** terminal on Windows:

```
python deborker.py
```

Pick the disk number, enter a label, and type `YES` to confirm.

**Warning:** this destroys everything on the selected disk. Double-check the
disk number before confirming.
