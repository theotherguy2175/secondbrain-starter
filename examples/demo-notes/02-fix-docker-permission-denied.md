---
title: "Fix: docker 'permission denied' on /var/run/docker.sock"
kind: fix
tags: [docker, linux]
scope: homelab
---
Error:

```
Got permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock
```

Fix: add your user to the `docker` group, then log out and back in.

```bash
sudo usermod -aG docker $USER
newgrp docker
```

Why it happens: the socket is owned by `root:docker`, and your user was not in that group.
