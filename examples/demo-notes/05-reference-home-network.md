---
title: Home network layout (example)
kind: reference
tags: [network, homelab]
scope: homelab
---
| VLAN | Purpose | Subnet |
|---|---|---|
| 10 | Servers | 192.168.10.0/24 |
| 20 | Trusted devices | 192.168.20.0/24 |
| 30 | IoT | 192.168.30.0/24 |
| 40 | Guests | 192.168.40.0/24 |

- Router/firewall: 192.168.10.1
- DNS: 192.168.10.2
- NAS: 192.168.10.5

IoT can reach the internet and Home Assistant only; it cannot reach the other VLANs.
