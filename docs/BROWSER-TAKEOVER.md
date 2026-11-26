# Browser with takeover (steps 1-3 built)

Goal: an agent can work in a real, **logged-in** browser (e.g. reply to a
community thread after the user approved the text), and the user can **take
over** that browser from the PWA to log in, enter a password or 2FA code,
or solve a captcha. Then the agent carries on. The agent never sees or
handles credentials.

Today agents only have a throwaway headless Chromium (Playwright MCP, fresh
profile per run), which is enough for testing a public app but not for
anything behind a login.

## Pieces

```
 PWA (chat)                 web (access gate)            browser-<project>
 ┌──────────────┐  WS/VNC  ┌──────────────────┐  :6080   ┌──────────────────────────┐
 │ "Übernehmen" ├─────────►│ /api/browser/... ├─────────►│ Xvfb + Chromium (headful)│
 │  live view   │          │ user-only route  │          │ x11vnc + websockify      │
 └──────────────┘          └──────────────────┘          │ CDP :9222 (internal only)│
                                                         │ profile volume (persist) │
 chat / worker agent ── Playwright MCP --cdp-endpoint ──►└──────────────────────────┘
```

1. **`agent-team-browser-<project>` container** — Chromium in a virtual
   display (Xvfb), its DevTools port (CDP) and a VNC→WebSocket bridge
   (websockify + noVNC files). One container per project, so sessions and
   cookies never cross projects.
   - Profile on a named volume: logins survive restarts.
   - Own bridge network, **internet only** (no LAN/host ports, same
     firewall rule set as other public-facing containers); reachable from
     the chat/worker containers on CDP and from web on the VNC socket only.
   - Memory limit (e.g. 2 GB) and a single browser instance.
2. **Agent side** — the Playwright MCP server already in the image, started
   with `--cdp-endpoint http://agent-team-browser-<project>:9222` instead
   of launching its own Chromium. Opt-in per channel/scout
   (`browser: shared` vs. today's `browser: true` = throwaway).
3. **Takeover in the PWA**
   - New user-only route in web: `/api/browser/<project>/vnc` proxies the
     WebSocket; the access gate keeps it away from internal callers (like
     `/api/system/*`).
   - Chat message type `takeover`: the agent posts it (new chat tool / API
     call "Bitte übernehmen: <reason>"), it lands in the inbox as
     *important* with a button. The button opens a full-screen noVNC view
     (works on the phone: keyboard, pinch zoom) and a "Fertig" button that
     resumes the agent's turn with "Der Nutzer ist fertig".
   - While the user has the view open, agent input is paused (a lock file /
     flag the MCP wrapper checks) so both never type at once.
4. **Approval for outward actions** — posting, commenting, messaging,
   signing up need the user's explicit "ja" for exactly that text and
   place in the chat. Enforced in the prompt *and* by a small guard: the
   MCP wrapper refuses clicks on submit-like controls unless the turn
   carries a fresh approval token from a button press.

## Rules the design keeps

- Passwords, 2FA codes and captchas: only the user, in the live view. The
  agent is told to stop and ask for takeover, never to try.
- No credential storage by agent-team; only the browser profile's cookies
  (on the volume, backed up encrypted like other volumes or excluded).
- Platform terms: many communities forbid automated posting. Default is
  draft-only; posting through the browser is an explicit per-post action.
- Screenshots the agent takes go to the task/chat artifacts, not to public
  places.

## Built so far

- `Dockerfile.browser` + `tools/browser-entrypoint.sh`: Xvfb, Chromium with
  the profile in `/profile`, x11vnc + noVNC on 6080, Playwright MCP over
  HTTP on 8931 (connected to Chromium on loopback, so CDP itself is never
  exposed). Env: `AT_BROWSER_MCP_FROM`, `AT_BROWSER_VIEW_FROM` (CIDRs
  allowed in), `AT_BROWSER_MCP_HOSTS` (Host header check of the MCP
  server), `AT_BROWSER_NO_SANDBOX`.
- The entrypoint sets the container's own iptables (needs `NET_ADMIN`,
  `SETUID`, `SETGID`, `CHOWN` at start; everything runs as uid 1000 with no
  capabilities afterwards): out only to public addresses, in only the
  configured ports/subnets. Disable IPv6 via sysctl.
- Chromium's sandbox needs unprivileged user namespaces, which hosts with
  AppArmor userns restrictions (and Docker's default seccomp profile) do not
  allow — then set `AT_BROWSER_NO_SANDBOX=1`; isolation is the container.
- Chat channel option `browser_mcp: http://<browser>:8931/mcp` gives that
  channel the tools `mcp__browser__*` plus prompt rules (takeover for
  logins/captchas, explicit approval for every outward action).

- Web app: `AT_BROWSER_VIEWS` (`{"<project>": "http://<browser>:6080"}`)
  enables `/browser/<project>/…` (noVNC files + WebSocket, user only — the
  socket route applies the access rules itself) and the chat button
  "Browser" on channels with `browser_mcp`. The browser container accepts
  6080 only from the web app's view network.

## Steps

1. ✅ Browser container + compose + network/firewall test. Until the PWA
   view exists, publish 6080 on the host's loopback and use an SSH tunnel.
2. ✅ Playwright MCP for one chat channel (`browser_mcp`).
3. ✅ Web route + PWA live view (chat button). The agent asks in the chat
   ("Bitte übernimm den Browser"), the user answers "fertig". Open: a
   dedicated takeover card in the inbox and the input lock while the view
   is open.
4. Approval token guard for submit-like actions.
5. Enable for scouts that need it.
