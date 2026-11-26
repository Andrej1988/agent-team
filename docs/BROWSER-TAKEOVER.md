# Browser with takeover (design, not built yet)

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

## Steps

1. Browser container + compose + network/firewall test (no PWA yet; the
   user can reach noVNC through an SSH tunnel to verify login persistence).
2. Playwright MCP over CDP for one chat channel (`browser: shared`).
3. Web route + PWA live view + takeover message/button + input lock.
4. Approval token guard for submit-like actions.
5. Enable for scouts that need it.
