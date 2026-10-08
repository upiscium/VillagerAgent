# K12 live containment qualification

The live adapter gate defines a deterministic, isolated 15-cell semantic
qualification using the frozen `K12Q-S*-T1-N1-*` order. It is operational,
non-scientific evidence and is always rejected by final scientific gates.

Containment probes P1–P4 use a separate campaign-level identity and output;
they are not qualification cells. Offline `/1` and qualification identities
are rejected by the typed final launch gate. On any stop, unattempted schedule
slots remain `not_started`; retry, resume, replacement, and same-cell relaunch
are forbidden.

This issue's adapters use only concrete scripted in-memory transports. Systemd
and RCON commands are constructed as inert tuple/string data for parser and
state-machine tests. There is no subprocess, process, network, RCON, systemd,
cgroup, or native-tool fallback.
