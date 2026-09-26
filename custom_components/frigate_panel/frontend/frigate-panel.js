/**
 * Frigate sidebar panel.
 *
 * Picks how to reach Frigate, then shows it in an iframe:
 *
 *   direct  - the browser loads Frigate straight from its own host. Nothing
 *             passes through Home Assistant, so it is as fast as Frigate is.
 *             Only possible when this client can actually reach that host.
 *   proxy   - everything goes through Home Assistant, which authenticates it
 *             and caches what it can. Works from anywhere Home Assistant does.
 *
 * Only the browser can tell these apart, which is why this decision is here
 * and not in Python.
 */

const PROBE_TIMEOUT_MS = 1500;
const IFRAME_LOAD_TIMEOUT_MS = 5000;

class FrigatePanel extends HTMLElement {
  constructor() {
    super();
    this._started = false;
    this._mode = null;
    this._narrow = false;
    this._hassSubpage = null;
    this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
    if (this._hassSubpage) {
      this._hassSubpage.hass = hass;
    }
    this._start();
  }

  set panel(panel) {
    this._panel = panel;
    this._start();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    if (this._hassSubpage) {
      this._hassSubpage.narrow = narrow;
    }
  }

  /** Run once, as soon as both hass and panel have been handed to us. */
  _start() {
    if (this._started || !this._hass || !this._panel) {
      return;
    }
    this._started = true;
    this._render();
    this._connect().catch((err) => {
      this._fail(`Could not open Frigate: ${err.message}`);
    });
  }

  _render() {
    const style = document.createElement("style");
    style.textContent = `
      /*
       * ha-panel-custom and partial-panel-resolver, both directly above us in
       * the tree, never declare a height on themselves -- only display:block
       * and padding. A percentage height has nothing definite to resolve
       * against there and collapses to the browser's ~150px default iframe
       * size. Home Assistant's own iframe-panel code hits the same problem
       * and works around it the same way: viewport units instead of a
       * percentage chain. 100dvh (falls back to 100vh where unsupported)
       * accounts for mobile browser chrome showing/hiding on scroll, which
       * plain vh does not.
       */
      :host { display: block; height: 100vh; height: 100dvh; }
      .wrap { position: relative; height: 100%; }
      iframe {
        display: block;
        width: 100%;
        height: 100%;
        border: 0;
        background: var(--primary-background-color, #fff);
      }
      .overlay {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 16px;
        text-align: center;
        color: var(--secondary-text-color, #666);
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
        background: var(--primary-background-color, #fff);
      }
      .overlay[hidden] { display: none; }
    `;

    this._frame = document.createElement("iframe");
    this._frame.setAttribute("allow", "fullscreen; autoplay; microphone");
    this._frame.setAttribute("allowfullscreen", "true");

    this._overlay = document.createElement("div");
    this._overlay.className = "overlay";
    this._overlay.textContent = "Connecting to Frigate…";

    // Held until _connect() knows whether it can wrap this in a toolbar --
    // appending straight to the shadow root would need undoing if the
    // toolbar shows up a moment later.
    this._wrap = document.createElement("div");
    this._wrap.className = "wrap";
    this._wrap.append(this._frame, this._overlay);
    this.shadowRoot.append(style);
  }

  async _connect() {
    const config = this._panel.config || {};

    // Run concurrently: neither depends on the other, and loading the
    // toolbar's component is a network fetch worth overlapping with the
    // direct-vs-proxy probe rather than adding to the wait.
    const [mode, subpage] = await Promise.all([
      this._chooseMode(config),
      this._prepareToolbar(config),
    ]);
    this._attachLayout(subpage);

    if (mode === "proxy") {
      // The iframe and everything it loads authenticate with a cookie, because
      // a browser cannot attach an Authorization header to its own asset
      // requests. Mint it before loading anything.
      await this._mintSession(config.proxy_base);
    }

    this._mode = mode;
    const src = mode === "direct" ? `${config.direct_url}/` : `${config.proxy_base}/`;
    this._load(src, mode, config);
  }

  /**
   * Build the "back to Home Assistant" toolbar, Home Assistant's own
   * `<hass-subpage main-page>` -- the same component its built-in
   * `panel_iframe:` uses for exactly this. Returns null if the option is off
   * or the component could not be loaded, in which case the panel falls back
   * to the bare iframe it always used.
   */
  async _prepareToolbar(config) {
    if (config.show_toolbar === false || !(await this._ensureHassSubpage())) {
      return null;
    }
    const subpage = document.createElement("hass-subpage");
    subpage.hass = this._hass;
    subpage.narrow = this._narrow;
    subpage.header = this._panel.title || "Frigate";
    // main-page, rather than a back arrow: there is no in-app page this panel
    // was pushed from, so the toolbar shows the menu button that reopens
    // Home Assistant's sidebar instead.
    subpage.mainPage = true;
    this._hassSubpage = subpage;
    return subpage;
  }

  /**
   * Make sure `<hass-subpage>` is registered before using it.
   *
   * Home Assistant code-splits it into whichever panel bundle first imports
   * it, so a session that goes straight from login to this panel may never
   * have loaded it. Its own built-in `panel_iframe:` panel type statically
   * imports hass-subpage, so asking the frontend's router to load *that*
   * panel's bundle pulls hass-subpage in as a side effect -- the same trick
   * lovelylain/hass_ingress uses for its own toolbar mode.
   *
   * `_getRoutes` is a private router method with no public equivalent for
   * this. If a future Home Assistant release renames or removes it, this
   * just returns false and the panel keeps working without the toolbar,
   * rather than breaking -- every caller already treats the toolbar as
   * optional.
   */
  async _ensureHassSubpage() {
    if (customElements.get("hass-subpage")) {
      return true;
    }
    try {
      const resolver = document.createElement("partial-panel-resolver");
      const getRoutes = resolver._getRoutes || resolver.getRoutes;
      if (typeof getRoutes !== "function") {
        return false;
      }
      const bootstrapPath = "__frigate_panel_bootstrap__";
      const { routes } = getRoutes.call(resolver, {
        [bootstrapPath]: { url_path: bootstrapPath, component_name: "iframe" },
      });
      await routes[bootstrapPath].load();
    } catch (err) {
      return false;
    }
    return !!customElements.get("hass-subpage");
  }

  /** Place the iframe wrapper inside the toolbar, or directly in the panel. */
  _attachLayout(subpage) {
    if (subpage) {
      subpage.append(this._wrap);
      this.shadowRoot.append(subpage);
    } else {
      this.shadowRoot.append(this._wrap);
    }
  }

  /**
   * Decide between direct and proxy.
   *
   * The result is remembered for the tab session so switching away from the
   * panel and back does not re-probe every time.
   */
  async _chooseMode(config) {
    const direct = config.direct_url;
    if (!config.allow_direct || !direct) {
      return "proxy";
    }

    // A page served over HTTPS may not frame an HTTP origin, and the browser
    // blocks the reachability probe too. Going direct here would render a
    // blank panel, so do not try.
    if (window.location.protocol === "https:" && direct.startsWith("http://")) {
      return "proxy";
    }

    const cacheKey = `frigate-panel:mode:${direct}`;
    const remembered = sessionStorage.getItem(cacheKey);
    if (remembered) {
      return remembered;
    }

    const mode = (await this._reachable(direct)) ? "direct" : "proxy";
    try {
      sessionStorage.setItem(cacheKey, mode);
    } catch (err) {
      // Private browsing can refuse storage; re-probing is an acceptable cost.
    }
    return mode;
  }

  /** Can this browser reach Frigate without going through Home Assistant? */
  async _reachable(baseUrl) {
    try {
      // no-cors gives an opaque response, which is enough: it resolves when
      // the host answered and rejects when it could not be reached.
      await fetch(`${baseUrl}/api/version`, {
        mode: "no-cors",
        cache: "no-store",
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      });
      return true;
    } catch (err) {
      return false;
    }
  }

  async _mintSession(proxyBase) {
    const post = (token) =>
      fetch(`${proxyBase}/-/session`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        credentials: "same-origin",
      });

    let response = await post(this._hass.auth.data.access_token);
    if (response.status === 401 && this._hass.auth.refreshAccessToken) {
      // The cached access token had expired.
      await this._hass.auth.refreshAccessToken();
      response = await post(this._hass.auth.data.access_token);
    }
    if (!response.ok) {
      throw new Error(`session request returned ${response.status}`);
    }
  }

  _load(src, mode, config) {
    let settled = false;
    const settle = () => {
      settled = true;
      this._overlay.hidden = true;
    };

    this._frame.addEventListener("load", settle, { once: true });

    // A cross-origin iframe that is refused (mixed content, CSP, a firewall)
    // never fires load, so fall back to the proxy rather than leaving the user
    // looking at an empty panel.
    window.setTimeout(() => {
      if (settled) {
        return;
      }
      if (mode === "direct") {
        sessionStorage.removeItem(`frigate-panel:mode:${config.direct_url}`);
        this._overlay.textContent = "Frigate not reachable directly, retrying…";
        this._mintSession(config.proxy_base)
          .then(() => this._load(`${config.proxy_base}/`, "proxy", config))
          .catch((err) => this._fail(`Could not open Frigate: ${err.message}`));
      } else {
        this._fail(
          "Frigate is not responding. Check that Home Assistant can reach it."
        );
      }
    }, IFRAME_LOAD_TIMEOUT_MS);

    this._frame.src = src;
  }

  _fail(message) {
    this._overlay.hidden = false;
    this._overlay.textContent = message;
  }
}

customElements.define("frigate-panel", FrigatePanel);
