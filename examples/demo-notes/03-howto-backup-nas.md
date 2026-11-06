---
title: "How to: nightly NAS backup with rsync"
kind: howto
tags: [backup, nas]
scope: homelab
---
1. Mount the NAS share at `/mnt/nas`.
2. Add a cron entry (runs at 02:30):

```
30 2 * * * rsync -a --delete /home/me/Documents/ /mnt/nas/backups/documents/
```

3. Check the last run with `ls -l /mnt/nas/backups/documents`.

Gotcha: `--delete` removes files on the NAS that you deleted locally. Remove that flag if you want a safety net.
