## Scout assignment — security (weekly)

Look at the system like an attacker who found it on the internet, and like a
careless insider. Read-only: no exploit attempts against running systems, no
requests that change data or trip alerts, and never print a secret — name only
where it is (file:line, key name).

Check, in this order:
1. **Exposure:** what is reachable from outside (published ports, tunnels,
   reverse proxies, CORS, debug and admin routes) and whether each one should be.
2. **Who may do what:** every route that changes data or shows private data —
   missing auth, ids taken from the request without an ownership check, internal
   endpoints trusting the network alone, sessions and tokens, password storage,
   rate limits on login, sign-up and expensive endpoints.
3. **Input:** SQL built from strings, shell calls with outside input, path
   traversal, unsafe deserialisation (pickle, `yaml.load`), SSRF in fetchers,
   HTML injection in templates and frontend, uploads.
4. **Secrets:** in code, git history, logs, images and compose files; file
   permissions; tokens with more rights than they need.
5. **Containers and host:** root users, privileged containers, Docker socket
   mounts, writable mounts that could be read-only, missing resource limits.
6. **Personal data:** stored, logged or kept longer than needed.

Dependency updates belong to the dependency scout; mention one only when it is
exploitable in the way this code uses it.

A proposal: title `Sicherheit: <what>`. Prio 1 when someone from the internet can
exploit it, 2 when it needs an inside foothold, 3 for hardening. Details:
- **Lücke:** what is wrong, with file:line or config
- **Angriff:** who could do what, how realistically, and what they would get
- **Fix:** the concrete change
- **Prüfen:** how to show it is closed

One proposal per root cause. No theoretical issues without an attack path, no
best-practice lists. Skip what "Open tasks" and "Proposed before" cover. Zero
proposals is a good outcome.

Write every proposal **in German**.
