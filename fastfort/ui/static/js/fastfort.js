/* FastFort admin behaviour.
 *
 * No framework, no build step. Everything here is progressive: with JavaScript
 * disabled the admin still navigates, searches, filters, sorts and paginates,
 * because all of that is server-rendered links and form submissions.
 *
 * What this adds is the part a server round trip cannot do well: remembering the
 * viewer's theme and sidebar preference, and keyboard shortcuts.
 */

(() => {
  "use strict";

  const STORE = {
    theme: "ff:theme",
    collapsed: "ff:sidebar-collapsed",
  };

  /* localStorage throws in private mode in some browsers, and a preference is
   * never worth breaking the page over. */
  const read = (key) => {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  };

  const write = (key, value) => {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* preferences are best-effort */
    }
  };

  const root = document.documentElement;

  // --- Theme ---------------------------------------------------------------

  const systemPrefersDark = () =>
    window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

  const currentTheme = () =>
    root.dataset.ffTheme || (systemPrefersDark() ? "dark" : "light");

  const applyTheme = (theme) => {
    root.dataset.ffTheme = theme;
    write(STORE.theme, theme);
    for (const icon of document.querySelectorAll("[data-ff-theme-icon]")) {
      icon.textContent = theme === "dark" ? "☀" : "☾";
    }
    // The switch in the account menu and the button in the topbar are two views
    // of one setting, so whichever is used, both have to end up showing it.
    for (const box of document.querySelectorAll("[data-ff-theme-switch]")) {
      box.checked = theme === "dark";
    }
  };

  const stored = read(STORE.theme);
  if (stored === "light" || stored === "dark") {
    applyTheme(stored);
  } else {
    // Reflect the effective theme in the icon without pinning the attribute,
    // so a later change of OS preference still takes effect.
    for (const icon of document.querySelectorAll("[data-ff-theme-icon]")) {
      icon.textContent = currentTheme() === "dark" ? "☀" : "☾";
    }
    for (const box of document.querySelectorAll("[data-ff-theme-switch]")) {
      box.checked = currentTheme() === "dark";
    }
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-ff-theme-toggle]")) {
      applyTheme(currentTheme() === "dark" ? "light" : "dark");
    }
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-ff-theme-switch]")) {
      applyTheme(event.target.checked ? "dark" : "light");
    }
  });

  // --- Sidebar -------------------------------------------------------------

  const app = document.getElementById("ff-app");

  if (app && read(STORE.collapsed) === "true") {
    app.dataset.collapsed = "true";
  }

  const setDrawer = (open) => {
    if (!app) return;
    app.dataset.drawer = open ? "open" : "closed";
    for (const button of document.querySelectorAll("[data-ff-drawer]")) {
      button.setAttribute("aria-expanded", String(open));
    }
    if (open) {
      if (!document.querySelector(".ff-scrim")) {
        const scrim = document.createElement("div");
        scrim.className = "ff-scrim";
        scrim.addEventListener("click", () => setDrawer(false));
        app.appendChild(scrim);
      }
    } else {
      document.querySelector(".ff-scrim")?.remove();
    }
  };

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-ff-drawer]")) {
      setDrawer(app?.dataset.drawer !== "open");
    }
  });

  // --- Keyboard ------------------------------------------------------------

  const isTyping = (element) =>
    element instanceof HTMLElement &&
    (element.isContentEditable ||
      ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName));

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setDrawer(false);
      return;
    }

    // Shortcuts must not fire while someone is typing into the search box.
    if (isTyping(document.activeElement) || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }

    if (event.key === "/") {
      const search = document.querySelector('input[type="search"]');
      if (search) {
        event.preventDefault();
        search.focus();
        search.select();
      }
      return;
    }

    if (event.key === "[") {
      if (!app) return;
      const collapsed = app.dataset.collapsed !== "true";
      app.dataset.collapsed = String(collapsed);
      write(STORE.collapsed, String(collapsed));
    }
  });
})();

/* Live list updates.
 *
 * Written here rather than by vendoring HTMX: the list view needs one behaviour
 * -- fetch a fragment and swap it -- and that is about sixty lines. Pulling in a
 * general-purpose library for it would add 14 KB, a dependency to keep audited,
 * and a second mental model for how the page updates.
 *
 * Strictly an enhancement. Every control is a real form input and every sort
 * header a real link, so with JavaScript off the same interactions still work as
 * ordinary GET requests.
 */

(() => {
  "use strict";

  const form = document.querySelector("[data-ff-live]");
  const target = form && document.querySelector(form.dataset.ffTarget || "#ff-results");
  if (!form || !target) return;

  const base = form.getAttribute("action") || window.location.pathname;
  let inFlight = null;

  const queryFrom = (extra) => {
    const params = new URLSearchParams(new FormData(form));
    for (const [key, value] of Object.entries(extra || {})) {
      if (value === null) params.delete(key);
      else params.set(key, value);
    }
    // Empty values would otherwise pile up in the URL as `?q=&is_active=`.
    for (const [key, value] of [...params]) {
      if (value === "") params.delete(key);
    }
    return params;
  };

  const load = async (params, { push = true } = {}) => {
    const url = params.toString() ? `${base}?${params}` : base;

    // A newer request supersedes an older one: typing quickly must not let an
    // earlier response land after a later one and show stale rows.
    inFlight?.abort();
    const controller = new AbortController();
    inFlight = controller;

    target.dataset.ffLoading = "true";
    target.setAttribute("aria-busy", "true");
    form.classList.add("htmx-request");

    try {
      const response = await fetch(url, {
        headers: { "X-FastFort-Partial": "results" },
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(String(response.status));
      target.innerHTML = await response.text();
      if (push) window.history.pushState({ ff: true }, "", url);
    } catch (error) {
      if (error.name === "AbortError") return;
      // Fall back to a full navigation. A list that silently stops responding is
      // worse than one that reloads.
      window.location.assign(url);
      return;
    } finally {
      if (inFlight === controller) {
        inFlight = null;
        delete target.dataset.ffLoading;
        target.setAttribute("aria-busy", "false");
        form.classList.remove("htmx-request");
      }
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    load(queryFrom({ p: null }));
  });

  // A changed dropdown or date bound applies immediately: making someone press
  // Apply after picking from a select is a step with no purpose.
  form.addEventListener("change", (event) => {
    if (event.target.matches("select, input[type='date'], input[type='datetime-local']")) {
      load(queryFrom({ p: null }));
    }
  });

  let typing;
  form.addEventListener("input", (event) => {
    if (!event.target.matches("input[type='search']")) return;
    clearTimeout(typing);
    // Long enough that a request is not sent per keystroke, short enough that
    // the results feel attached to the typing.
    typing = setTimeout(() => load(queryFrom({ p: null })), 250);
  });

  // Sorting and pagination are links inside the swapped fragment, so they are
  // caught by delegation rather than bound after every update.
  target.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || link.target) return;

    const url = new URL(link.href, window.location.origin);
    if (url.pathname !== base) return; // a row link leaves the list

    event.preventDefault();
    load(url.searchParams);
  });

  // Back and forward must move through the list, not out of it.
  window.addEventListener("popstate", () => {
    load(new URLSearchParams(window.location.search), { push: false });
  });
})();
