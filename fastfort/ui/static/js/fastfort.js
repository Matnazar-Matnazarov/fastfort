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
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-ff-theme-toggle]")) {
      applyTheme(currentTheme() === "dark" ? "light" : "dark");
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
