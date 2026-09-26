# Frigate Panel

Puts Frigate in the Home Assistant sidebar, proxied through Home Assistant's
authentication, with a server-side cache in front of it.

This is separate from the official
[frigate-hass-integration](https://github.com/blakeblackshear/frigate-hass-integration),
which gives you cameras, sensors and media browsing but **registers no sidebar
panel at all**. Run both: that one for entities, this one for the UI.

## What it does

- **Sidebar entry** that opens the full Frigate UI.
- **Works remotely.** Everything is proxied through Home Assistant, so the
  panel works anywhere Home Assistant does — no need to expose Frigate.
- **Loads Frigate directly when it can.** If your browser can reach Frigate
  itself, the panel skips the proxy entirely.
- **Caches what cannot change**, on disk, shared by every client and surviving
  restarts.
- **Never caches live video.** Live view, recordings and HLS are relayed
  unbuffered.

## Install

1. Copy `custom_components/frigate_panel` into your Home Assistant
   `config/custom_components/`, or add this repo to HACS as a custom
   repository.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Frigate Panel**, and
   enter your Frigate URL (e.g. `http://192.168.1.10:5000`).

## What actually gets cached

The honest version, because the answer is narrower than "caching makes it fast":

| What | Cached? | Why |
|---|---|---|
| Event thumbnails, snapshots | **Yes**, 10 min default | Frigate sends *no* cache headers for these, so browsers re-fetch every one on every scroll. This is the big win. |
| Event clips, review previews | **Yes**, long | Only written once the event has ended. |
| UI bundle (`/assets/`), locales | **Yes** | Frigate marks these immutable; caching them means a cold browser doesn't wake Frigate. |
| API JSON (`/api/config`, event lists) | **No** | Frigate marks it `no-store` because it changes. Overriding that would show you stale events. |
| Live view, recordings, HLS, websockets | **No** | Streams. Relayed straight through. |

A detail worth knowing: Frigate already tells browsers its UI bundle is
immutable, so a warm browser cache was never the slow part. What this cache
fixes is the **cold** case, **event media** (which nothing was caching), and
**multiple clients** all pulling the same bytes through Frigate.

Every proxied response carries `X-Frigate-Panel-Cache: hit`, `miss` or
`bypass`, so you can check this yourself in your browser's network tab.

## Options

**Settings → Devices & Services → Frigate Panel → Configure**

| Option | Default | Notes |
|---|---|---|
| Sidebar name | `Frigate` | |
| Sidebar icon | `mdi:cctv` | Any [Material Design Icon](https://pictogrammers.com/library/mdi/). |
| Show a way back to Home Assistant | on | A small header with a menu button, so you can reopen the sidebar. Turn off for a fully immersive kiosk display. |
| Load Frigate directly when possible | on | See the caveat below. |
| Direct Frigate URL | your Frigate URL | The address *browsers* use on your LAN. |
| Cache size limit | 512 MB | Oldest entries evicted first. `0` disables the cache. |
| Thumbnail/snapshot cache time | 600 s | Keep it short enough that in-progress events still refresh. |

### The direct-load caveat

Direct loading often **cannot** apply, and the reason is a browser rule, not a
bug here: **a page served over HTTPS may not embed an HTTP origin.** So if you
reach Home Assistant over HTTPS (Nabu Casa, a reverse proxy) and Frigate is
plain HTTP on your LAN, the panel detects that and stays on the proxy.

Direct loading works when Home Assistant is HTTP on the LAN, or when Frigate
has a valid HTTPS certificate. Otherwise you get the proxy, which is the safe
default and still cached.

### The "way back" toolbar

Frigate's own UI has no link back to Home Assistant, and a custom panel like
this one gets the whole viewport with none of Home Assistant's own chrome
around it — so on a phone, where the sidebar is a hidden drawer rather than a
permanent column, there was no way out of the panel except closing the tab.

The toolbar adds a small header with Home Assistant's own menu button, which
reopens the sidebar. It reuses Home Assistant's `<hass-subpage>` component —
the same one its built-in `panel_iframe:` uses — rather than a bar built from
scratch here.

One tradeoff worth knowing: Home Assistant only loads that component's code
when some panel that uses it has already been visited in the session. To make
it available even on a session that goes straight from login to this panel,
the frontend nudges Home Assistant's router to load it, via the same private
API [lovelylain/hass_ingress](https://github.com/lovelylain/hass_ingress) uses
for its own `ui_mode: toolbar`. It's not a public, guaranteed-stable API — if
a future Home Assistant release changes it, the toolbar just stops appearing
rather than breaking the panel; toggle it off if that ever happens and you'd
rather not see the gap.

## How the authentication works

An iframe cannot attach an `Authorization` header to the requests it makes for
its own assets, and Home Assistant's HTTP auth supports only bearer tokens and
individually signed URLs — neither of which a single-page app can use.

So this integration does what Home Assistant's own Supervisor ingress does:

1. The panel calls `POST /api/frigate_panel/<entry>/-/session` with your bearer
   token.
2. That returns a random session token as an `HttpOnly`, `SameSite=Strict`
   cookie scoped to the proxy path.
3. Further requests present the cookie.

The session records the Home Assistant refresh token that created it, and the
proxy re-checks that credential on **every** request — so revoking a session in
Home Assistant immediately kills panel access, rather than leaving a cookie
valid for the rest of its lifetime. Your Home Assistant token is never
forwarded to Frigate.

Anyone who can open the panel sees Frigate through one shared upstream
connection; the proxy does not give different Home Assistant users different
Frigate permissions.

## Requirements and limits

- Tested against Home Assistant **2026.9.3** on Python 3.14.
- One Frigate instance per Home Assistant. A second would need its own panel
  URL and cache namespace.
- Requires Frigate's `X-Ingress-Path` support (the same mechanism its Home
  Assistant add-on uses) so it rebases its asset URLs under the proxy. Frigate
  0.13+.
- If Frigate has its own authentication enabled, you log in to Frigate once
  inside the panel; its cookie persists.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install homeassistant pytest-homeassistant-custom-component \
    home-assistant-frontend hass-web-proxy-lib ruff
.venv/bin/python -m pytest
.venv/bin/python -m ruff check custom_components tests
```

The cache, the path rules and the session store import no Home Assistant code
and are unit-tested directly. The proxy, panel and config flow are tested
against a real Home Assistant instance, and websocket proxying is tested
against a real upstream aiohttp server.
