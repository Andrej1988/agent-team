"""Host metrics collector (CPU, memory, disks, temperatures, GPUs, SMART,
24 h histories) as its own small service, so the internet-facing web app
needs no device or host access. The web app proxies /api/system/host/...
to it (AT_SYSMON_URL)."""
