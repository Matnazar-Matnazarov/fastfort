/* FastFort admin behaviour.
 *
 * No framework, no build step, no dependencies. Roughly the surface of a small
 * component library, written directly against the DOM because that is cheaper
 * here than the alternative: an admin that ships React would be a 140 KB
 * download, a build pipeline in the wheel, and a supply chain to audit, in
 * exchange for behaviour that is a few hundred lines of event listeners.
 *
 * Everything is progressive. With JavaScript off the admin still navigates,
 * searches, filters, sorts, paginates, creates, edits and deletes, because all
 * of that is server-rendered links and form submissions. What this adds is the
 * part a round trip cannot do well: controls the operating system refuses to
 * style, feedback without a page load, and the keyboard.
 *
 * Structure:
 *   core      - storage, dom helpers, the enhance() registry
 *   shell     - theme, sidebar, menus, global keys
 *   combobox  - the replacement for <select> and <select multiple>
 *   overlay   - modals, confirmation, toasts
 *   palette   - Ctrl+K
 *   listing   - row selection, bulk actions, live updates
 */

(() => {
  "use strict";

  // =========================================================================
  // Core
  // =========================================================================

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

  /* Marks the document as scripted, so controls that only work with JavaScript
   * are hidden from anyone who does not have it rather than sitting there doing
   * nothing when clicked -- and so the native controls these widgets replace can
   * be hidden only once their replacement actually exists. */
  root.dataset.ffJs = "1";

  const el = (tag, attributes = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
      else node.setAttribute(name, value === true ? "" : String(value));
    }
    for (const child of [].concat(children)) {
      if (child) node.append(child);
    }
    return node;
  };

  /* An icon from the sprite the page already inlined. Cloning a <use> is free;
   * there is no second request and no glyph font to wait for. */
  const icon = (name, size = 16) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "ff-icon");
    svg.setAttribute("width", size);
    svg.setAttribute("height", size);
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#ff-i-${name}`);
    svg.append(use);
    return svg;
  };

  const uid = (() => {
    let n = 0;
    return (prefix) => `${prefix}-${++n}`;
  })();

  const t = (key) => root.dataset[`ffT${key}`] || FALLBACK_TEXT[key] || key;

  /* The interface strings script has to produce on its own. Written onto <html>
   * by the server in the active language; these are the fallbacks for anything
   * a project's own translation file has not covered. */
  const FALLBACK_TEXT = {
    Search: "Search…",
    NoResults: "No results",
    Selected: "selected",
    Clear: "Clear",
    Cancel: "Cancel",
    Delete: "Delete",
    Loading: "Loading…",
    Choose: "Choose…",
    Showing: "Showing the first {n}. Keep typing to narrow it down.",
    Today: "Today",
    Previous: "Previous",
    Next: "Next",
    Days: "Days",
    Hours: "Hours",
    Minutes: "Minutes",
    Seconds: "Seconds",
    ZoomIn: "Zoom in",
    ZoomOut: "Zoom out",
  };

  /* Every widget registers here. Called once on load and again on any fragment
   * the live list swaps in, so a control in a freshly loaded page of rows is
   * upgraded exactly like one that arrived with the document. */
  const enhancers = [];
  const enhance = (scope = document) => {
    for (const run of enhancers) {
      try {
        run(scope);
      } catch (error) {
        // One broken widget must not stop the rest of the page from working.
        console.error("[fastfort]", error);
      }
    }
  };

  /* `once` marks a node as already upgraded. Re-running enhance() over a scope
   * that contains both new and old nodes is the normal case, not the exception. */
  const once = (node, flag) => {
    if (node.dataset[flag] === "1") return false;
    node.dataset[flag] = "1";
    return true;
  };

  const isTyping = (element) =>
    element instanceof HTMLElement &&
    (element.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName));

  /* Places a panel below its trigger, or above when there is not enough room
   * below. Measured rather than assumed: a filter at the foot of a long list
   * would otherwise open a list that runs off the bottom of the window. */
  const place = (panel, trigger) => {
    panel.removeAttribute("data-ff-place");
    const below = window.innerHeight - trigger.getBoundingClientRect().bottom;
    if (below < Math.min(panel.offsetHeight + 16, 220)) {
      panel.setAttribute("data-ff-place", "above");
    }
  };

  /* Whether a click passed through an element matching `selector`, for the
   * document-level "a click landed outside this panel, so close it" checks
   * scattered through this file.
   *
   * `composedPath()` rather than walking up from `event.target` with
   * `.closest()`: several of these panels re-render part of themselves the
   * moment something inside them is clicked -- picking a month in the date
   * picker rebuilds its grid, choosing a tag in a multi-select combobox
   * rebuilds its option list -- and that rebuild can detach `event.target`
   * from the document before this same click finishes bubbling up here.
   * `.closest()` on a detached node always comes back empty, which reads as
   * "that click was outside the panel" the instant it was a click inside it:
   * the date picker closed itself on "next month", and a multi-select closed
   * itself after every single choice. `composedPath()` is captured when the
   * event is dispatched, before anything has a chance to mutate the tree, so
   * it keeps answering correctly regardless of what the click handled inside
   * the panel went on to do to it. */
  const clickWasInside = (event, selector) =>
    event.composedPath().some((node) => node instanceof Element && node.matches(selector));

  window.FastFort = { enhance, icon, el, t };

  // =========================================================================
  // Shell: theme, sidebar, menus, global keys
  // =========================================================================

  /* Three modes, not two. A light/dark switch can only ever say one of them, so
   * touching it pins the admin and there is no way back to following the
   * operating system -- a setting nobody chose and cannot undo. "system" is the
   * absence of the attribute, which is what the stylesheet's media query keys
   * off; writing data-ff-theme="system" would match neither branch. */
  const MODES = new Set(["light", "dark", "system"]);

  const systemPrefersDark = () =>
    window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

  const storedMode = () => {
    const value = read(STORE.theme);
    return MODES.has(value) ? value : null;
  };

  /* boot.js has already put the stored choice on <html>; this only has to know
   * which of the three it was, so the account menu can show it as pressed.
   * Falls back to whatever the server rendered, so a page opened for the first
   * time reflects the project's configured theme rather than assuming light. */
  let mode = storedMode() ?? (MODES.has(root.dataset.ffTheme) ? root.dataset.ffTheme : "system");

  const effectiveTheme = () => (mode === "system" ? (systemPrefersDark() ? "dark" : "light") : mode);

  const syncThemeControls = () => {
    for (const button of document.querySelectorAll("[data-ff-theme-set]")) {
      button.setAttribute("aria-pressed", String(button.dataset.ffThemeSet === mode));
    }
  };

  const applyTheme = (next) => {
    if (!MODES.has(next)) return;
    mode = next;
    if (next === "system") delete root.dataset.ffTheme;
    else root.dataset.ffTheme = next;
    write(STORE.theme, next);
    syncThemeControls();
  };

  applyTheme(mode);

  document.addEventListener("click", (event) => {
    const option = event.target.closest("[data-ff-theme-set]");
    if (option) {
      applyTheme(option.dataset.ffThemeSet);
      return;
    }
    // The quick toggle flips between the two visible states. Cycling through
    // three from one button would leave people unable to predict what a click
    // does, so the third lives in the account menu where it is labelled.
    if (event.target.closest("[data-ff-theme-toggle]")) {
      applyTheme(effectiveTheme() === "dark" ? "light" : "dark");
    }
  });

  // --- Disclosure menus ----------------------------------------------------

  /* <details> closes on its own summary but not on a click elsewhere, which
   * leaves a menu hanging open behind whatever the person went on to do. */
  const closeDetailsMenus = (except) => {
    for (const menu of document.querySelectorAll("details[data-ff-account][open]")) {
      if (menu !== except) menu.open = false;
    }
  };

  document.addEventListener("click", (event) => {
    closeDetailsMenus(event.target.closest("details[data-ff-account]"));
  });

  // --- Dropdown menus ------------------------------------------------------

  /* A button plus a panel, rather than <details>: these open over a table row,
   * need to flip above the trigger near the foot of the page, and have to close
   * when another one opens. */
  let openMenu = null;

  const closeMenu = () => {
    if (!openMenu) return;
    openMenu.panel.hidden = true;
    openMenu.trigger.setAttribute("aria-expanded", "false");
    openMenu = null;
  };

  const openMenuFor = (trigger, panel) => {
    closeMenu();
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    place(panel, trigger);
    openMenu = { trigger, panel };
  };

  enhancers.push((scope) => {
    for (const trigger of scope.querySelectorAll("[data-ff-menu]")) {
      if (!once(trigger, "ffMenuReady")) continue;
      const panel = trigger.parentElement.querySelector(".ff-dropdown__panel");
      if (!panel) continue;
      panel.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      trigger.setAttribute("aria-haspopup", "menu");

      trigger.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (openMenu?.trigger === trigger) closeMenu();
        else openMenuFor(trigger, panel);
      });

      // A row that navigates or submits takes the menu with it. Anything else
      // in the panel does not: a date filter's two inputs live in one of these,
      // and closing on the first click would make the second bound unreachable.
      panel.addEventListener("click", (event) => {
        if (event.target.closest(".ff-item, [data-ff-close]")) closeMenu();
      });
    }
  });

  document.addEventListener("click", (event) => {
    if (openMenu && !clickWasInside(event, ".ff-dropdown")) closeMenu();
  });

  // --- Sidebar -------------------------------------------------------------

  const app = document.getElementById("ff-app");

  const setDrawer = (open) => {
    if (!app) return;
    app.dataset.drawer = open ? "open" : "closed";
    for (const button of document.querySelectorAll("[data-ff-drawer]")) {
      button.setAttribute("aria-expanded", String(open));
    }
    if (open) {
      if (!document.querySelector(".ff-scrim")) {
        const scrim = el("div", { class: "ff-scrim", onclick: () => setDrawer(false) });
        app.appendChild(scrim);
      }
    } else {
      document.querySelector(".ff-scrim")?.remove();
    }
  };

  /* On the root, not on `.ff-app`, so boot.js can set it before the first paint
   * -- `.ff-app` does not exist that early. Expanding removes the attribute
   * rather than setting it to "false", so the stylesheet has one state to match
   * instead of two spellings of the same thing. */
  const toggleCollapsed = () => {
    const collapsed = root.dataset.ffCollapsed !== "true";
    if (collapsed) root.dataset.ffCollapsed = "true";
    else delete root.dataset.ffCollapsed;
    write(STORE.collapsed, String(collapsed));
    for (const button of document.querySelectorAll("[data-ff-collapse]")) {
      button.setAttribute("aria-pressed", String(collapsed));
    }
  };

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-ff-drawer]")) {
      setDrawer(app?.dataset.drawer !== "open");
    } else if (event.target.closest("[data-ff-collapse]")) {
      toggleCollapsed();
    }
  });

  // --- Language filter -----------------------------------------------------

  /* Nine languages is past the point where a list is read rather than scanned.
   * Matching the name and the code, so both "Deutsch" and "de" find German --
   * someone who knows the code is not going to guess the endonym. */
  document.addEventListener("input", (event) => {
    const box = event.target.closest("[data-ff-lang-filter]");
    if (!box) return;

    const form = box.closest("[data-ff-langs]");
    const term = box.value.trim().toLowerCase();
    let shown = 0;
    for (const row of form.querySelectorAll("[data-ff-lang]")) {
      const matches = !term || row.dataset.ffLang.includes(term);
      row.hidden = !matches;
      if (matches) shown += 1;
    }
    const empty = form.querySelector(".ff-langs__empty");
    if (empty) empty.hidden = shown > 0;
  });

  // --- Appearance ----------------------------------------------------------

  /* Accent and density, alongside the theme and the sidebar above. All four are
   * per-person and live in local storage: the project's settings decide what a
   * new visitor sees, these decide what this one does. boot.js re-applies them
   * before the first paint, which is why nothing here has to survive a reload. */
  const applyAccent = (hue) => {
    const value = Number(hue);
    if (!Number.isFinite(value) || value < 0 || value > 360) return;
    root.style.setProperty("--ff-h", String(value));
    write("ff:accent", String(value));
    syncAppearance();
  };

  const applyPrimary = (style) => {
    if (style !== "neutral" && style !== "accent") return;
    if (style === "accent") root.dataset.ffPrimary = "accent";
    else delete root.dataset.ffPrimary;
    write("ff:primary", style);
    syncAppearance();
  };

  const applyDensity = (density) => {
    if (density !== "comfortable" && density !== "compact") return;
    root.dataset.ffDensity = density;
    write("ff:density", density);
    syncAppearance();
  };

  const setCollapsed = (collapsed) => {
    if (collapsed) root.dataset.ffCollapsed = "true";
    else delete root.dataset.ffCollapsed;
    write(STORE.collapsed, String(collapsed));
    syncAppearance();
  };

  /* Every control in the panel reads its state back off the document, so the
   * panel cannot disagree with the page it is describing. */
  const syncAppearance = () => {
    syncThemeControls();
    const hue = root.style.getPropertyValue("--ff-h").trim();
    for (const swatch of document.querySelectorAll("[data-ff-accent]")) {
      swatch.setAttribute("aria-pressed", String(swatch.dataset.ffAccent === hue));
    }
    const primary = root.dataset.ffPrimary || "neutral";
    for (const button of document.querySelectorAll("[data-ff-primary-set]")) {
      button.setAttribute("aria-pressed", String(button.dataset.ffPrimarySet === primary));
    }
    const density = root.dataset.ffDensity || "comfortable";
    for (const button of document.querySelectorAll("[data-ff-density-set]")) {
      button.setAttribute("aria-pressed", String(button.dataset.ffDensitySet === density));
    }
    const collapsed = String(root.dataset.ffCollapsed === "true");
    for (const button of document.querySelectorAll("[data-ff-collapse-set]")) {
      button.setAttribute("aria-pressed", String(button.dataset.ffCollapseSet === collapsed));
    }
  };

  const setSettings = (open) => {
    const panel = document.getElementById("ff-settings-panel");
    if (!panel) return;
    panel.hidden = !open;
    for (const button of document.querySelectorAll("[data-ff-settings]")) {
      button.setAttribute("aria-expanded", String(open));
    }
    if (open) {
      syncAppearance();
      if (!document.querySelector(".ff-scrim--settings")) {
        document.body.append(
          el("div", { class: "ff-scrim ff-scrim--settings", onclick: () => setSettings(false) })
        );
      }
    } else {
      document.querySelector(".ff-scrim--settings")?.remove();
    }
  };

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-ff-settings]")) {
      setSettings(Boolean(document.getElementById("ff-settings-panel")?.hidden));
      return;
    }
    if (event.target.closest("[data-ff-settings-close]")) {
      setSettings(false);
      return;
    }

    const swatch = event.target.closest("[data-ff-accent]");
    if (swatch) {
      applyAccent(swatch.dataset.ffAccent);
      return;
    }

    const primary = event.target.closest("[data-ff-primary-set]");
    if (primary) {
      applyPrimary(primary.dataset.ffPrimarySet);
      return;
    }

    const density = event.target.closest("[data-ff-density-set]");
    if (density) {
      applyDensity(density.dataset.ffDensitySet);
      return;
    }

    const collapse = event.target.closest("[data-ff-collapse-set]");
    if (collapse) {
      setCollapsed(collapse.dataset.ffCollapseSet === "true");
      return;
    }

    if (event.target.closest("[data-ff-appearance-reset]")) {
      // Back to whatever the project configured, which is what the server
      // rendered before any of this ran.
      root.style.removeProperty("--ff-h");
      delete root.dataset.ffCollapsed;
      delete root.dataset.ffPrimary;
      root.dataset.ffDensity = "comfortable";
      for (const key of ["ff:accent", "ff:density", "ff:primary", "ff:theme", STORE.collapsed]) {
        try {
          window.localStorage.removeItem(key);
        } catch {
          /* preferences are best-effort */
        }
      }
      applyTheme("system");
      syncAppearance();
    }
  });

  // --- Global keys ---------------------------------------------------------

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (openMenu) {
        closeMenu();
        return;
      }
      if (!document.getElementById("ff-settings-panel")?.hidden) {
        setSettings(false);
        return;
      }
      if (!document.getElementById("ff-filter-panel")?.hidden) {
        setFilters(false);
        return;
      }
      setDrawer(false);
      closeDetailsMenus();
      return;
    }

    // Ctrl+K works while typing, because it is how you leave wherever you are.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openPalette();
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

    if (event.key === "[") toggleCollapsed();
  });

  // =========================================================================
  // Combobox
  //
  // Replaces `<select>` and `<select multiple>`. The native element stays in the
  // DOM and is still what submits -- this only takes over the presentation and
  // the keyboard. Three modes:
  //
  //   static    every option is already in the select
  //   remote    options come from an autocomplete endpoint as you type
  //   multiple  a many-to-many selection, drawn as removable chips
  //
  // Written rather than vendored because Select2 and friends are a jQuery
  // dependency, a stylesheet that fights this one, and about 70 KB, for a
  // control whose whole job is a filtered list and eight key bindings.
  // =========================================================================

  class Combobox {
    constructor(select) {
      this.select = select;
      this.multiple = select.multiple;
      this.url = select.dataset.ffUrl || null;
      this.placeholder = select.dataset.ffPlaceholder || t("Choose");
      this.searchable =
        select.dataset.ffSearch === "always" || Boolean(this.url) || select.options.length > 7;
      this.clearable = select.dataset.ffClearable !== "false" && !this.multiple;
      this.id = uid("ff-combo");
      this.options = [];
      this.active = -1;
      this.open = false;
      this.footer = null;
      this.build();
    }

    // -- construction -----------------------------------------------------

    build() {
      const select = this.select;
      select.classList.add("ff-combo__native");
      select.setAttribute("tabindex", "-1");
      select.setAttribute("aria-hidden", "true");

      this.rootEl = el("div", { class: "ff-combo" });
      select.parentNode.insertBefore(this.rootEl, select);
      this.rootEl.append(select);

      this.trigger = this.multiple ? this.buildChips() : this.buildButton();
      this.panel = this.buildPanel();
      this.rootEl.append(this.panel);

      // The label that pointed at the native select has to follow it, or
      // clicking the label focuses a control nobody can see.
      const label = select.id && document.querySelector(`label[for="${CSS.escape(select.id)}"]`);
      if (label) {
        label.addEventListener("click", (event) => {
          event.preventDefault();
          this.trigger.focus();
        });
      }

      this.readNative();
      this.bind();
    }

    buildButton() {
      this.valueEl = el("span", { class: "ff-combo__value" });
      this.clearEl = this.clearable
        ? el("span", { class: "ff-combo__clear", "aria-hidden": "true" }, [icon("close", 12)])
        : null;
      const button = el(
        "button",
        {
          type: "button",
          class: "ff-combo__button",
          id: `${this.id}-trigger`,
          role: "combobox",
          "aria-expanded": "false",
          "aria-haspopup": "listbox",
          "aria-controls": `${this.id}-list`,
          disabled: this.select.disabled,
        },
        [this.valueEl, this.clearEl, icon("chevron-down", 14)]
      );
      button.lastChild.classList.add("ff-combo__caret");
      this.rootEl.append(button);
      return button;
    }

    /* The chips are the trigger's own content, so clicking anywhere in the box
     * opens the list -- except on a chip's remove control, which is handled in
     * `bind` before the click reaches the toggle. */
    buildChips() {
      const box = el("button", {
        type: "button",
        class: "ff-chips",
        id: `${this.id}-trigger`,
        role: "combobox",
        "aria-expanded": "false",
        "aria-haspopup": "listbox",
        "aria-controls": `${this.id}-list`,
      });
      this.chipsEl = box;
      this.rootEl.append(box);
      return box;
    }

    buildPanel() {
      this.listEl = el("div", {
        class: "ff-combo__list",
        id: `${this.id}-list`,
        role: "listbox",
        "aria-multiselectable": this.multiple ? "true" : null,
      });

      const children = [];
      if (this.searchable) {
        this.searchEl = el("input", {
          type: "text",
          autocomplete: "off",
          spellcheck: "false",
          placeholder: t("Search"),
          "aria-label": t("Search"),
          "aria-controls": `${this.id}-list`,
        });
        children.push(el("div", { class: "ff-combo__search" }, [icon("search", 14), this.searchEl]));
      }
      children.push(this.listEl);

      return el(
        "div",
        { class: "ff-pop ff-combo__panel", hidden: true },
        children
      );
    }

    // -- state ------------------------------------------------------------

    /* The native element is the source of truth for what is selected. Reading
     * back from it rather than keeping a parallel copy means a value set by
     * anything else -- a reset, another script -- cannot desynchronise. */
    readNative() {
      this.options = [...this.select.options].map((option) => ({
        value: option.value,
        label: option.textContent.trim(),
        selected: option.selected,
        placeholder: option.value === "",
      }));
      this.render();
    }

    get selected() {
      return this.options.filter((option) => option.selected && !option.placeholder);
    }

    render() {
      if (this.multiple) this.renderChips();
      else this.renderValue();
    }

    renderValue() {
      const chosen = this.selected[0];
      this.valueEl.textContent = chosen ? chosen.label : this.placeholder;
      this.valueEl.dataset.ffEmpty = chosen ? "false" : "true";
      if (this.clearEl) this.clearEl.hidden = !chosen;
    }

    renderChips() {
      this.chipsEl.replaceChildren();
      const chosen = this.selected;
      if (!chosen.length) {
        this.chipsEl.append(el("span", { class: "ff-chips__placeholder", text: this.placeholder }));
        return;
      }
      for (const option of chosen) {
        const remove = el("span", {
          class: "ff-chip__remove",
          role: "button",
          "data-ff-remove": option.value,
          "aria-label": `${t("Clear")}: ${option.label}`,
        });
        remove.append(icon("close", 10));
        this.chipsEl.append(
          el("span", { class: "ff-chip" }, [
            el("span", { class: "ff-chip__label", text: option.label }),
            remove,
          ])
        );
      }
    }

    // -- the list ---------------------------------------------------------

    renderList(items, note) {
      this.listEl.replaceChildren();
      this.active = -1;
      this.rows = [];
      this.trigger.removeAttribute("aria-activedescendant");

      if (note) {
        this.listEl.append(el("div", { class: "ff-combo__note", text: note }));
        return;
      }
      if (!items.length) {
        this.listEl.append(el("div", { class: "ff-combo__note", text: t("NoResults") }));
        return;
      }

      items.forEach((option, index) => {
        const row = el(
          "div",
          {
            class: "ff-combo__option",
            role: "option",
            id: `${this.id}-o${index}`,
            "aria-selected": String(Boolean(option.selected)),
            "data-ff-value": option.value,
          },
          [
            el("span", { class: "ff-combo__option__mark" }, [icon("check", 14)]),
            el("span", { class: "ff-combo__option__label", text: option.label }),
          ]
        );
        this.listEl.append(row);
      });
      this.rows = [...this.listEl.querySelectorAll(".ff-combo__option")];
      this.setActive(items.findIndex((option) => option.selected));
    }

    setActive(index) {
      if (!this.rows?.length) return;
      const rows = this.rows;
      if (this.active >= 0 && rows[this.active]) delete rows[this.active].dataset.ffActive;
      this.active = Math.max(0, Math.min(index, rows.length - 1));
      const row = rows[this.active];
      row.dataset.ffActive = "true";
      row.scrollIntoView({ block: "nearest" });
      this.trigger.setAttribute("aria-activedescendant", row.id);
    }

    filtered() {
      const term = (this.searchEl?.value || "").trim().toLowerCase();
      const shown = this.options.filter((option) => !option.placeholder);
      if (!term) return shown;
      return shown.filter((option) => option.label.toLowerCase().includes(term));
    }

    // -- remote -----------------------------------------------------------

    /* A relation with more rows than a dropdown can hold is searched on the
     * server instead of shipped to the browser. The alternative -- rendering
     * every row as an <option> -- is a megabyte of HTML on a table of 50,000
     * customers, which is what makes stock Django admin unusable at that size. */
    async fetchRemote(term) {
      this.requestId = (this.requestId || 0) + 1;
      const id = this.requestId;
      this.renderList([], t("Loading"));

      try {
        const url = new URL(this.url, window.location.origin);
        url.searchParams.set("q", term);
        const response = await fetch(url, {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error(String(response.status));
        const payload = await response.json();
        if (id !== this.requestId) return; // superseded by a later keystroke

        const chosen = new Map(this.selected.map((option) => [option.value, option.label]));
        const items = payload.results.map((option) => ({
          value: String(option.value),
          label: option.label,
          selected: chosen.has(String(option.value)),
        }));
        this.renderList(items);

        if (payload.more) {
          this.listEl.append(
            el("div", {
              class: "ff-combo__footer",
              text: t("Showing").replace("{n}", payload.results.length),
            })
          );
        }
      } catch {
        if (id === this.requestId) this.renderList([], t("NoResults"));
      }
    }

    refreshList() {
      if (this.url) {
        clearTimeout(this.typing);
        this.typing = setTimeout(() => this.fetchRemote(this.searchEl?.value || ""), 200);
      } else {
        this.renderList(this.filtered());
      }
    }

    // -- selection --------------------------------------------------------

    /* Remote options are not in the native select until they are chosen, so
     * choosing one has to add it. Without this a foreign key picked from an
     * autocomplete would submit nothing. */
    ensureOption(value, label) {
      let option = [...this.select.options].find((candidate) => candidate.value === value);
      if (!option) {
        option = new Option(label, value);
        this.select.append(option);
      }
      return option;
    }

    choose(value, label) {
      const option = this.ensureOption(value, label);
      if (this.multiple) {
        option.selected = !option.selected;
      } else {
        this.select.value = value;
      }
      this.select.dispatchEvent(new Event("change", { bubbles: true }));
      this.readNative();
      if (this.multiple) this.refreshList();
      else this.close();
    }

    clear() {
      if (this.multiple) {
        for (const option of this.select.options) option.selected = false;
      } else {
        this.select.value = "";
      }
      this.select.dispatchEvent(new Event("change", { bubbles: true }));
      this.readNative();
    }

    remove(value) {
      const option = [...this.select.options].find((candidate) => candidate.value === value);
      if (!option) return;
      option.selected = false;
      this.select.dispatchEvent(new Event("change", { bubbles: true }));
      this.readNative();
    }

    // -- open and close ---------------------------------------------------

    show() {
      if (this.open) return;
      closeCombos(this);
      closeMenu();
      this.open = true;
      this.rootEl.dataset.ffOpen = "true";
      this.panel.hidden = false;
      this.trigger.setAttribute("aria-expanded", "true");
      if (this.searchEl) this.searchEl.value = "";
      this.refreshList();
      place(this.panel, this.trigger);
      this.searchEl?.focus();
    }

    close() {
      if (!this.open) return;
      this.open = false;
      delete this.rootEl.dataset.ffOpen;
      this.panel.hidden = true;
      this.trigger.setAttribute("aria-expanded", "false");
      this.trigger.removeAttribute("aria-activedescendant");
    }

    toggle() {
      if (this.open) this.close();
      else this.show();
    }

    // -- events -----------------------------------------------------------

    bind() {
      this.trigger.addEventListener("click", (event) => {
        // A chip's remove control sits inside the trigger; it must not also
        // open the list.
        const removal = event.target.closest("[data-ff-remove]");
        if (removal) {
          event.preventDefault();
          event.stopPropagation();
          this.remove(removal.dataset.ffRemove);
          return;
        }
        if (this.clearEl && event.target.closest(".ff-combo__clear")) {
          event.preventDefault();
          event.stopPropagation();
          this.clear();
          return;
        }
        event.preventDefault();
        this.toggle();
      });

      this.trigger.addEventListener("keydown", (event) => this.onTriggerKey(event));
      this.searchEl?.addEventListener("input", () => this.refreshList());
      this.searchEl?.addEventListener("keydown", (event) => this.onListKey(event));

      this.listEl.addEventListener("click", (event) => {
        const row = event.target.closest(".ff-combo__option");
        if (!row) return;
        this.choose(row.dataset.ffValue, row.querySelector(".ff-combo__option__label").textContent);
      });

      // Hover moves the highlight, so the mouse and the keyboard agree on which
      // row Enter would take.
      this.listEl.addEventListener("mousemove", (event) => {
        const row = event.target.closest(".ff-combo__option");
        if (row && this.rows) this.setActive(this.rows.indexOf(row));
      });

      // Anything that changes the native element behind our back -- a form
      // reset, another widget -- is reflected back into the display.
      this.select.addEventListener("ff:sync", () => this.readNative());
    }

    onTriggerKey(event) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
        event.preventDefault();
        this.show();
      } else if (event.key === "Escape") {
        this.close();
      } else if (event.key === "Backspace" && this.multiple) {
        const chosen = this.selected;
        if (chosen.length) this.remove(chosen[chosen.length - 1].value);
      } else if (event.key.length === 1 && this.searchable) {
        // Typing straight at a closed control opens it and starts the search,
        // which is what a native select does and what the fingers expect.
        this.show();
        if (this.searchEl) this.searchEl.value = event.key;
        this.refreshList();
        event.preventDefault();
      }
    }

    onListKey(event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.setActive(this.active + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        this.setActive(this.active - 1);
      } else if (event.key === "Home") {
        event.preventDefault();
        this.setActive(0);
      } else if (event.key === "End") {
        event.preventDefault();
        this.setActive((this.rows?.length || 1) - 1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        const row = this.rows?.[this.active];
        if (row) {
          this.choose(row.dataset.ffValue, row.querySelector(".ff-combo__option__label").textContent);
        }
      } else if (event.key === "Escape") {
        event.preventDefault();
        this.close();
        this.trigger.focus();
      } else if (event.key === "Tab") {
        this.close();
      }
    }
  }

  const combos = new Set();

  const closeCombos = (except) => {
    for (const combo of combos) {
      if (combo !== except) combo.close();
    }
  };

  enhancers.push((scope) => {
    for (const select of scope.querySelectorAll("select[data-ff-combo]")) {
      if (!once(select, "ffComboReady")) continue;
      combos.add(new Combobox(select));
    }
  });

  document.addEventListener("click", (event) => {
    if (!clickWasInside(event, ".ff-combo")) closeCombos();
  });

  // =========================================================================
  // Colour
  //
  // The swatch and the hex box are one control wearing two faces, so each has
  // to follow the other. Only the text box has a name, so only it submits --
  // which also means a value the picker cannot represent survives being typed.
  // =========================================================================

  const HEX = /^#[0-9a-f]{6}$/i;

  document.addEventListener("input", (event) => {
    const group = event.target.closest("[data-ff-color]");
    if (!group) return;

    const swatch = group.querySelector('input[type="color"]');
    const hex = group.querySelector(".ff-color__hex");

    if (event.target === swatch) {
      hex.value = swatch.value.toUpperCase();
    } else if (HEX.test(hex.value.trim())) {
      swatch.value = hex.value.trim().toLowerCase();
    }
  });


  // =========================================================================
  // Date picker
  //
  // The native picker is drawn by the operating system: a different shape on
  // every platform, unstyleable, and on Linux a grey box that belongs to no
  // design system -- the same objection as the native `<select>`, for the same
  // reason.
  //
  // The native input stays. It holds the value, validates it, and is what
  // submits, so with script off this is exactly the control it has always been.
  // Only the picker is replaced.
  //
  // Month and weekday names come from `Intl`, keyed off the page's own `lang`,
  // so this needs no strings of its own in nine catalogues and is right for
  // locales nobody here has thought about.
  // =========================================================================

  const DAY_MS = 86_400_000;

  const iso = (date) =>
    `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(
      date.getDate()
    ).padStart(2, "0")}`;

  /* Parsed as local time on purpose. `new Date("2026-03-01")` is parsed as UTC
   * and lands on the previous day for anyone west of Greenwich, which is the
   * classic off-by-one in every hand-rolled picker. */
  const parseISO = (text) => {
    const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(text || "");
    if (!match) return null;
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    return Number.isNaN(date.getTime()) ? null : date;
  };

  class DatePicker {
    constructor(input) {
      this.input = input;
      this.withTime = input.type === "datetime-local";
      this.locale = root.lang || "en";
      this.id = uid("ff-cal");
      this.open = false;
      this.build();
    }

    /* Monday in most of the world, Sunday in the US and a few others. `weekInfo`
     * knows; where it is missing, Monday is the better guess for an admin. */
    get firstDay() {
      try {
        const info = new Intl.Locale(this.locale).weekInfo;
        return (info?.firstDay ?? 1) % 7;
      } catch {
        return 1;
      }
    }

    build() {
      const wrapper = el("div", { class: "ff-datefield" });
      this.input.parentNode.insertBefore(wrapper, this.input);
      wrapper.append(this.input);

      this.trigger = el(
        "button",
        {
          type: "button",
          class: "ff-datefield__button",
          "aria-haspopup": "dialog",
          "aria-expanded": "false",
          "aria-label": t("Choose"),
        },
        [icon("calendar", 15)]
      );
      wrapper.append(this.trigger);

      this.panel = el("div", { class: "ff-pop ff-datepicker", role: "dialog", hidden: true });
      wrapper.append(this.panel);

      this.trigger.addEventListener("click", (event) => {
        event.preventDefault();
        this.toggle();
      });
      this.panel.addEventListener("click", (event) => this.onClick(event));
      this.panel.addEventListener("keydown", (event) => this.onKey(event));
    }

    // -- rendering --------------------------------------------------------

    get value() {
      return parseISO(this.input.value);
    }

    render() {
      const shown = this.cursor;
      const monthName = new Intl.DateTimeFormat(this.locale, {
        month: "long",
        year: "numeric",
      }).format(shown);

      this.panel.replaceChildren(
        el("div", { class: "ff-datepicker__head" }, [
          el(
            "button",
            {
              type: "button",
              class: "ff-action",
              "data-ff-cal-step": "-1",
              "aria-label": t("Previous"),
            },
            [icon("chevron-left", 15)]
          ),
          el("span", { class: "ff-datepicker__month", text: monthName }),
          el(
            "button",
            {
              type: "button",
              class: "ff-action",
              "data-ff-cal-step": "1",
              "aria-label": t("Next"),
            },
            [icon("chevron-right", 15)]
          ),
        ]),
        this.grid(shown),
        el("div", { class: "ff-datepicker__foot" }, [
          el("button", {
            type: "button",
            class: "ff-btn ff-btn--sm ff-btn--ghost",
            "data-ff-cal-clear": "1",
            text: t("Clear"),
          }),
          el("button", {
            type: "button",
            class: "ff-btn ff-btn--sm",
            "data-ff-cal-today": "1",
            text: t("Today"),
          }),
        ])
      );
    }

    grid(shown) {
      const first = new Date(shown.getFullYear(), shown.getMonth(), 1);
      const offset = (first.getDay() - this.firstDay + 7) % 7;
      const start = new Date(first.getTime() - offset * DAY_MS);

      const weekdays = new Intl.DateTimeFormat(this.locale, { weekday: "short" });
      const header = el("div", { class: "ff-datepicker__week" });
      for (let index = 0; index < 7; index += 1) {
        const day = new Date(start.getTime() + index * DAY_MS);
        header.append(el("span", { text: weekdays.format(day).slice(0, 2) }));
      }

      const grid = el("div", { class: "ff-datepicker__days", role: "grid" });
      const selected = this.value;
      const today = iso(new Date());

      // Six rows always, so the panel does not change height as months change
      // and the buttons underneath do not move out from under the pointer.
      for (let index = 0; index < 42; index += 1) {
        const day = new Date(start.getTime() + index * DAY_MS);
        const stamp = iso(day);
        grid.append(
          el("button", {
            type: "button",
            class: "ff-datepicker__day",
            "data-ff-cal-pick": stamp,
            "data-ff-outside": day.getMonth() !== shown.getMonth() ? "true" : null,
            "aria-current": stamp === today ? "date" : null,
            "aria-selected": selected && stamp === iso(selected) ? "true" : "false",
            tabindex: stamp === iso(this.focused) ? "0" : "-1",
            text: String(day.getDate()),
          })
        );
      }

      return el("div", {}, [header, grid]);
    }

    // -- behaviour --------------------------------------------------------

    pick(stamp) {
      const chosen = parseISO(stamp);
      if (!chosen) return;

      if (this.withTime) {
        // Keep whatever time was already there: someone correcting the date of
        // an appointment has not asked to move it to midnight.
        const existing = /T(\d{2}:\d{2})/.exec(this.input.value);
        this.input.value = `${stamp}T${existing ? existing[1] : "00:00"}`;
      } else {
        this.input.value = stamp;
      }

      this.input.dispatchEvent(new Event("change", { bubbles: true }));
      this.close();
      this.input.focus();
    }

    show() {
      if (this.open) return;
      closeDatePickers(this);
      this.open = true;
      this.cursor = this.value ?? new Date();
      this.focused = this.value ?? new Date();
      this.panel.hidden = false;
      this.trigger.setAttribute("aria-expanded", "true");
      this.render();
      place(this.panel, this.trigger);
      this.panel.querySelector('[tabindex="0"]')?.focus();
    }

    close() {
      if (!this.open) return;
      this.open = false;
      this.panel.hidden = true;
      this.trigger.setAttribute("aria-expanded", "false");
    }

    toggle() {
      if (this.open) this.close();
      else this.show();
    }

    step(months) {
      this.cursor = new Date(this.cursor.getFullYear(), this.cursor.getMonth() + months, 1);
      this.render();
    }

    onClick(event) {
      const stepper = event.target.closest("[data-ff-cal-step]");
      if (stepper) {
        this.step(Number(stepper.dataset.ffCalStep));
        return;
      }
      if (event.target.closest("[data-ff-cal-clear]")) {
        this.input.value = "";
        this.input.dispatchEvent(new Event("change", { bubbles: true }));
        this.close();
        return;
      }
      if (event.target.closest("[data-ff-cal-today]")) {
        this.pick(iso(new Date()));
        return;
      }
      const day = event.target.closest("[data-ff-cal-pick]");
      if (day) this.pick(day.dataset.ffCalPick);
    }

    /* Arrow keys walk the grid a day or a week at a time, which is how a
     * calendar is navigated without a mouse. Moving off the shown month follows
     * into the next one rather than stopping at its edge. */
    onKey(event) {
      const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
      const move = moves[event.key];

      if (move) {
        event.preventDefault();
        this.focused = new Date(this.focused.getTime() + move * DAY_MS);
        this.cursor = new Date(this.focused.getFullYear(), this.focused.getMonth(), 1);
        this.render();
        this.panel.querySelector('[tabindex="0"]')?.focus();
        return;
      }
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        this.pick(iso(this.focused));
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        this.close();
        this.trigger.focus();
      }
    }
  }

  const pickers = new Set();

  const closeDatePickers = (except) => {
    for (const picker of pickers) {
      if (picker !== except) picker.close();
    }
  };

  enhancers.push((scope) => {
    for (const input of scope.querySelectorAll("input[data-ff-date]")) {
      if (!once(input, "ffDateReady")) continue;
      pickers.add(new DatePicker(input));
    }
  });

  document.addEventListener("click", (event) => {
    if (!clickWasInside(event, ".ff-datefield")) closeDatePickers();
  });

  // =========================================================================
  // Duration
  //
  // A timedelta has no HTML control, so the field was a text box with the help
  // text "Length of time, as HH:MM:SS -- or 2d HH:MM:SS for more than a day."
  // Asking somebody to type a format, and then rejecting what they typed, is
  // the part worth removing: four labelled boxes cannot be written wrong.
  //
  // The text input stays and stays named, so the server keeps parsing the one
  // format it always did and the field still works with script off.
  // =========================================================================

  const SEGMENTS = [
    { key: "days", label: "Days", short: "d", max: null },
    { key: "hours", label: "Hours", short: "h", max: 23 },
    { key: "minutes", label: "Minutes", short: "m", max: 59 },
    { key: "seconds", label: "Seconds", short: "s", max: 59 },
  ];

  /* Reads what the server rendered. Anything unparseable is left alone: the
   * text box is shown instead, so a value this does not understand can still be
   * read and corrected rather than being silently zeroed.
   *
   * Fractional seconds are deliberately not matched. Whole-second boxes cannot
   * show `00:00:01.5`, and rounding it to fit would throw away part of a stored
   * value the moment somebody opened the row and pressed Save. */
  const parseDuration = (text) => {
    const match = /^\s*(?:(\d+)\s*d\s*)?(\d+):([0-5]\d)(?::([0-5]\d))?\s*$/.exec(text || "");
    if (!match) return null;
    const [, days, first, second, third] = match;
    // Without seconds the clock is MM:SS, matching how the server parses it.
    const parts =
      third === undefined
        ? { hours: 0, minutes: Number(first), seconds: Number(second) }
        : { hours: Number(first), minutes: Number(second), seconds: Number(third) };
    return { days: Number(days || 0), ...parts };
  };

  class Duration {
    constructor(input) {
      this.input = input;
      this.boxes = new Map();
      this.build();
    }

    build() {
      const parsed = parseDuration(this.input.value);
      const field = el("div", { class: "ff-duration" });

      for (const segment of SEGMENTS) {
        const box = el("input", {
          type: "number",
          class: "ff-input ff-duration__box",
          inputmode: "numeric",
          min: "0",
          value: String(parsed ? parsed[segment.key] : 0),
          "aria-label": t(segment.label),
        });
        if (segment.max !== null) box.max = String(segment.max);

        // Typing past a segment's ceiling should carry, the way it does in
        // every clock control -- 90 minutes is an hour and a half, not an error.
        box.addEventListener("input", () => this.sync());
        box.addEventListener("change", () => {
          this.carry();
          this.sync();
        });
        box.addEventListener("keydown", (event) => {
          if (event.key !== "Enter") return;
          // Otherwise Enter in a number box submits the form mid-edit.
          event.preventDefault();
          this.carry();
          this.sync();
        });

        this.boxes.set(segment.key, box);
        field.append(
          el("label", { class: "ff-duration__part" }, [
            box,
            el("span", { class: "ff-duration__unit", text: t(segment.label) }),
          ])
        );
      }

      this.input.after(field);
      // Unparseable values keep the text box: see `parseDuration`.
      if (parsed) {
        this.input.type = "hidden";
        this.field = field;
      } else {
        field.remove();
      }
    }

    value(key) {
      const raw = Math.trunc(Number(this.boxes.get(key).value));
      return Number.isFinite(raw) && raw > 0 ? raw : 0;
    }

    carry() {
      let total =
        this.value("days") * 86_400 +
        this.value("hours") * 3_600 +
        this.value("minutes") * 60 +
        this.value("seconds");
      for (const segment of [...SEGMENTS].reverse()) {
        const size = { seconds: 60, minutes: 60, hours: 24, days: Infinity }[segment.key];
        const carried = size === Infinity ? total : total % size;
        this.boxes.get(segment.key).value = String(carried);
        total = size === Infinity ? 0 : Math.floor(total / size);
      }
    }

    sync() {
      const pad = (key) => String(this.value(key)).padStart(2, "0");
      const clock = `${pad("hours")}:${pad("minutes")}:${pad("seconds")}`;
      const days = this.value("days");
      this.input.value = days ? `${days}d ${clock}` : clock;
    }
  }

  enhancers.push((scope) => {
    for (const input of scope.querySelectorAll("input[data-ff-duration]")) {
      if (!once(input, "ffDurationReady")) continue;
      new Duration(input);
    }
  });

  // =========================================================================
  // Map
  //
  // A pair of coordinates is a number nobody can check. "51.5074, -0.1278" is
  // either the right place or a transposed pair a thousand miles away, and the
  // only way to tell is to look at it.
  //
  // Tiles are images from whatever server `UISettings.map_tile_url` names, so
  // the map only exists when a project configured one -- the admin's CSP starts
  // at `default-src 'none'` and that setting is what adds the host to it. With
  // no URL, or no script, the field is the text box it always was.
  //
  // Web Mercator, the same projection every tile service uses, so the arithmetic
  // is the standard one and tiles from any of them line up.
  // =========================================================================

  const TILE = 256;
  const MAX_ZOOM = 19;
  const MIN_ZOOM = 1;
  /* The projection is undefined at the poles and grows without bound towards
   * them; every slippy map clamps at the latitude that makes the world square. */
  const MAX_LAT = 85.05112878;

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

  const lngToX = (lng, zoom) => ((lng + 180) / 360) * TILE * 2 ** zoom;

  const latToY = (lat, zoom) => {
    const radians = (clamp(lat, -MAX_LAT, MAX_LAT) * Math.PI) / 180;
    const merc = Math.log(Math.tan(Math.PI / 4 + radians / 2));
    return (0.5 - merc / (2 * Math.PI)) * TILE * 2 ** zoom;
  };

  const xToLng = (x, zoom) => (x / (TILE * 2 ** zoom)) * 360 - 180;

  const yToLat = (y, zoom) => {
    const merc = (0.5 - y / (TILE * 2 ** zoom)) * 2 * Math.PI;
    return (Math.atan(Math.sinh(merc)) * 180) / Math.PI;
  };

  /* Accepts what the server renders and what a person pastes: "41.2995,
   * 69.2401", with or without the space. Anything else means "no point yet",
   * which is a normal state for a nullable column. */
  const parsePoint = (text) => {
    const match = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/.exec(text || "");
    if (!match) return null;
    const lat = Number(match[1]);
    const lng = Number(match[2]);
    if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return null;
    return { lat, lng };
  };

  /* Six decimals is about 10cm. More digits is false precision and a value that
   * no longer round-trips through the text box unchanged. */
  const formatPoint = ({ lat, lng }) => `${lat.toFixed(6)}, ${lng.toFixed(6)}`;

  class PointMap {
    constructor(input) {
      this.input = input;
      this.template = input.dataset.ffMap;
      this.point = parsePoint(input.value);
      const fallback = parsePoint(input.dataset.ffMapCenter) || { lat: 0, lng: 0 };
      this.center = this.point || fallback;
      // Zoomed out until there is a point: a street-level view of nowhere in
      // particular is less use than being able to see which continent it is.
      this.zoom = this.point ? 13 : 2;
      this.build();
    }

    build() {
      this.tiles = el("div", { class: "ff-map__tiles" });
      this.marker = el("div", { class: "ff-map__marker", "aria-hidden": "true" });
      this.canvas = el("div", { class: "ff-map__canvas" }, [this.tiles, this.marker]);

      const zoomIn = el(
        "button",
        { type: "button", class: "ff-action", "aria-label": t("ZoomIn") },
        [icon("plus", 15)]
      );
      const zoomOut = el(
        "button",
        { type: "button", class: "ff-action", "aria-label": t("ZoomOut") },
        [icon("minus", 15)]
      );
      zoomIn.addEventListener("click", () => this.zoomBy(1));
      zoomOut.addEventListener("click", () => this.zoomBy(-1));

      this.element = el("div", { class: "ff-map ff-js-only" }, [
        this.canvas,
        el("div", { class: "ff-map__controls" }, [zoomIn, zoomOut]),
      ]);

      this.input.after(this.element);
      this.bind();
      // The container has no width until it is in the document.
      this.draw();
      // A form field can be inside a panel that is hidden at build time, where
      // every tile would be laid out against a width of zero.
      new ResizeObserver(() => this.draw()).observe(this.canvas);

      this.input.addEventListener("change", () => {
        // `pickAt` writes the same value into this input and dispatches this
        // very event so anyone bound to the field learns the value changed.
        // Without this guard, that dispatch would loop back here and recentre
        // the map on the point that was just clicked -- so every click both
        // dropped a pin and yanked the whole map to put that pin in the middle,
        // which reads as the map lurching on every click rather than a marker
        // appearing where the pointer was.
        if (this.settingInput) return;
        const typed = parsePoint(this.input.value);
        if (!typed) return;
        this.point = typed;
        this.center = typed;
        this.draw();
      });
    }

    bind() {
      let dragging = null;

      this.canvas.addEventListener("pointerdown", (event) => {
        dragging = { x: event.clientX, y: event.clientY, moved: false };
        this.canvas.setPointerCapture(event.pointerId);
      });

      this.canvas.addEventListener("pointermove", (event) => {
        if (!dragging) return;
        const dx = event.clientX - dragging.x;
        const dy = event.clientY - dragging.y;
        // A click is a press that did not travel; without a threshold, the
        // shake in anybody's hand turns every drag into a dropped pin.
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragging.moved = true;
        dragging.x = event.clientX;
        dragging.y = event.clientY;
        this.panBy(-dx, -dy);
      });

      this.canvas.addEventListener("pointerup", (event) => {
        const wasDrag = dragging?.moved;
        dragging = null;
        this.canvas.releasePointerCapture(event.pointerId);
        if (!wasDrag) this.pickAt(event);
      });

      this.canvas.addEventListener("pointercancel", () => {
        dragging = null;
      });

      this.canvas.addEventListener(
        "wheel",
        (event) => {
          event.preventDefault();
          this.zoomBy(event.deltaY < 0 ? 1 : -1, event);
        },
        { passive: false }
      );
    }

    size() {
      const box = this.canvas.getBoundingClientRect();
      return { width: box.width, height: box.height };
    }

    panBy(dx, dy) {
      const { width, height } = this.size();
      const x = lngToX(this.center.lng, this.zoom) + dx;
      const y = latToY(this.center.lat, this.zoom) + dy;
      const span = TILE * 2 ** this.zoom;
      this.center = {
        lat: yToLat(clamp(y, height / 2, span - height / 2), this.zoom),
        lng: xToLng(clamp(x, width / 2, span - width / 2), this.zoom),
      };
      this.draw();
    }

    zoomBy(step, event) {
      const next = clamp(this.zoom + step, MIN_ZOOM, MAX_ZOOM);
      if (next === this.zoom) return;
      // Zoom towards the pointer, so the place under it stays under it.
      if (event) {
        const at = this.atEvent(event);
        this.zoom = next;
        const after = this.atEvent(event);
        this.center = {
          lat: this.center.lat + (at.lat - after.lat),
          lng: this.center.lng + (at.lng - after.lng),
        };
      } else {
        this.zoom = next;
      }
      this.draw();
    }

    atEvent(event) {
      const box = this.canvas.getBoundingClientRect();
      const originX = lngToX(this.center.lng, this.zoom) - box.width / 2;
      const originY = latToY(this.center.lat, this.zoom) - box.height / 2;
      return {
        lat: yToLat(originY + (event.clientY - box.top), this.zoom),
        lng: xToLng(originX + (event.clientX - box.left), this.zoom),
      };
    }

    pickAt(event) {
      this.point = this.atEvent(event);
      this.input.value = formatPoint(this.point);
      // Synchronous: every listener the dispatch reaches, including the one
      // this class registers on the same input, runs before this returns.
      this.settingInput = true;
      this.input.dispatchEvent(new Event("change", { bubbles: true }));
      this.settingInput = false;
      this.draw();
    }

    draw() {
      const { width, height } = this.size();
      if (!width || !height) return;

      // Identifies which call to draw() a pending tile load belongs to, so a
      // slow load from an earlier call cannot sweep away tiles a later call
      // has since decided to keep -- see the guard in `settle` below.
      const generation = (this.generation = (this.generation ?? 0) + 1);

      const span = 2 ** this.zoom;
      const originX = lngToX(this.center.lng, this.zoom) - width / 2;
      const originY = latToY(this.center.lat, this.zoom) - height / 2;
      const first = { x: Math.floor(originX / TILE), y: Math.floor(originY / TILE) };
      const last = {
        x: Math.floor((originX + width) / TILE),
        y: Math.floor((originY + height) / TILE),
      };

      const wanted = new Map();
      for (let x = first.x; x <= last.x; x += 1) {
        for (let y = first.y; y <= last.y; y += 1) {
          // Rows past the edge do not exist; columns wrap, because the world does.
          if (y < 0 || y >= span) continue;
          const column = ((x % span) + span) % span;
          // Keyed by zoom too: two different zoom levels can share the same
          // x,y numbers, and without the prefix a tile from the wrong zoom
          // would be repositioned rather than replaced.
          wanted.set(`${this.zoom}:${x},${y}`, {
            url: this.template
              .replace("{z}", String(this.zoom))
              .replace("{x}", String(column))
              .replace("{y}", String(y)),
            left: x * TILE - originX,
            top: y * TILE - originY,
          });
        }
      }

      // Reuse the images already on screen. Rebuilding them all on every pointer
      // move would re-request each tile and make dragging flicker.
      this.shown ??= new Map();

      // Panning keeps most keys, so nothing here needs to wait: the sweep of
      // whatever is no longer wanted can run right away. Zooming replaces every
      // key at once, and removing a whole screen of tiles before their
      // replacements have loaded is what "flashes empty, then repaints" looks
      // like -- so a zoom's stale tiles are swept only once every tile it
      // requested has either loaded or failed, and stay on screen as a
      // placeholder (in the wrong position, but filled) until then.
      let awaited = 0;
      const sweep = () => {
        // A slower, earlier draw() finishing after a newer one has already run
        // must not act: its `wanted` is stale, and applying it here would
        // remove tiles the newer call has since decided to keep. The newer
        // call's own sweep -- guarded the same way, and unconditional once it
        // is the one still current -- is what actually cleans up.
        if (generation !== this.generation) return;
        for (const [key, image] of this.shown) {
          if (!wanted.has(key)) {
            image.remove();
            this.shown.delete(key);
          }
        }
      };

      for (const [key, tile] of wanted) {
        let image = this.shown.get(key);
        if (!image) {
          image = el("img", { class: "ff-map__tile", alt: "", loading: "lazy" });
          awaited += 1;
          const settle = () => {
            awaited -= 1;
            image.dataset.ffLoaded = "true";
            if (awaited === 0) sweep();
          };
          image.addEventListener("load", settle, { once: true });
          image.addEventListener("error", settle, { once: true });
          image.src = tile.url;
          this.tiles.append(image);
          this.shown.set(key, image);
        }
        image.style.transform = `translate(${tile.left}px, ${tile.top}px)`;
      }

      // Every tile this call wanted was already on screen -- a pan within the
      // loaded area, most often -- so there is nothing to wait for.
      if (awaited === 0) sweep();

      if (this.point) {
        this.marker.hidden = false;
        this.marker.style.transform =
          `translate(${lngToX(this.point.lng, this.zoom) - originX}px, ` +
          `${latToY(this.point.lat, this.zoom) - originY}px)`;
      } else {
        this.marker.hidden = true;
      }
    }
  }

  enhancers.push((scope) => {
    for (const input of scope.querySelectorAll("input[data-ff-map]")) {
      if (!once(input, "ffMapReady")) continue;
      new PointMap(input);
    }
  });

  // =========================================================================
  // Related objects
  //
  // The buttons beside a foreign key: open what is chosen, or create a new one
  // without abandoning the form being filled in. Django has had this for twenty
  // years and it is the thing people miss first -- without it, "the category
  // does not exist yet" means losing the half-filled form you are on.
  // =========================================================================

  const RELATED_FLAG = "ffRelatedReady";

  /* The popup writes its result into the page it opens as, and that page's copy
   * of this script hands it back here. `window.opener` rather than postMessage
   * because both windows are the same origin by construction -- the popup is an
   * admin URL -- and a message channel would need a target origin to check that
   * is already known to be identical. */
  window.ffRelatedResult = (field, value, label) => {
    const select = document.querySelector(`[data-ff-related="${CSS.escape(field)}"]`)
      ?.closest(".ff-with-related")
      ?.querySelector("select");
    if (!select) return;

    let option = [...select.options].find((candidate) => candidate.value === value);
    if (!option) {
      option = new Option(label, value);
      select.append(option);
    }
    option.textContent = label;

    if (select.multiple) option.selected = true;
    else select.value = value;

    // The combobox reads its state back off the native element, so it has to be
    // told the element changed underneath it.
    select.dispatchEvent(new CustomEvent("ff:sync"));
    select.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const relatedSelect = (group) =>
    group.closest(".ff-with-related")?.querySelector("select") ?? null;

  const syncRelated = (group) => {
    const select = relatedSelect(group);
    if (!select) return;
    // "Change" and "view" need something to act on. A multi-valued picker has no
    // single subject, so they stay off there and only "add" applies.
    const chosen = select.multiple
      ? [...select.selectedOptions].map((option) => option.value).filter(Boolean)
      : [select.value].filter(Boolean);
    const one = chosen.length === 1 ? chosen[0] : "";

    for (const button of group.querySelectorAll("[data-ff-related-action]")) {
      if (button.dataset.ffRelatedAction === "add") continue;
      button.disabled = !one;
      button.dataset.ffRelatedKey = one;
    }
  };

  enhancers.push((scope) => {
    for (const group of scope.querySelectorAll("[data-ff-related]")) {
      if (!once(group, RELATED_FLAG)) continue;
      const select = relatedSelect(group);
      select?.addEventListener("change", () => syncRelated(group));
      syncRelated(group);
    }
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-ff-related-action]");
    if (!button) return;
    event.preventDefault();

    const group = button.closest("[data-ff-related]");
    const action = button.dataset.ffRelatedAction;
    const field = group.dataset.ffRelated;
    const key = button.dataset.ffRelatedKey;

    let url;
    if (action === "add") {
      url = `${group.dataset.ffRelatedAdd}?_popup=1&_field=${encodeURIComponent(field)}`;
    } else if (!key) {
      return;
    } else if (action === "change") {
      url = `${group.dataset.ffRelatedBase}/${encodeURIComponent(key)}/?_popup=1&_field=${encodeURIComponent(field)}`;
    } else {
      // "View" is the same record without the popup contract: it opens the row
      // in a tab of its own, which is where someone reading rather than editing
      // wants it.
      window.open(`${group.dataset.ffRelatedBase}/${encodeURIComponent(key)}/`, "_blank");
      return;
    }

    window.open(url, "ff-related", "width=900,height=700,resizable=yes,scrollbars=yes");
  });

  /* "Cancel" in a popup closes the window it was opened as. A plain link would
   * navigate that same window to the parent's list instead, leaving a popup
   * open on a page nothing on the opener's side is waiting for -- the form was
   * abandoned, not saved, so there is nothing to hand back and nowhere useful
   * left for this window to go. Falls back to following the link if this was
   * somehow not opened as a popup, so the button still does something. */
  document.addEventListener("click", (event) => {
    const cancel = event.target.closest("[data-ff-popup-cancel]");
    if (!cancel) return;
    if (window.opener && !window.opener.closed) {
      event.preventDefault();
      window.close();
    }
  });

  /* This runs in the popup, on the page the save returned. */
  const result = document.getElementById("ff-popup-result");
  if (result && window.opener && !window.opener.closed) {
    window.opener.ffRelatedResult?.(
      result.dataset.ffField,
      result.dataset.ffValue,
      result.dataset.ffLabel
    );
    window.close();
  }

  // =========================================================================
  // Overlays: modals, confirmation, toasts
  // =========================================================================

  /* Destructive actions get a dialog rather than a page. The confirmation page
   * still exists and is still what a link points at, so this is an accelerator
   * and not a requirement -- but a full navigation to answer one yes/no
   * question, then a navigation back, is two page loads to delete one row. */
  const confirmDialog = (options) =>
    new Promise((resolve) => {
      const dialog = el("dialog", { class: "ff-modal" });
      const close = (answer) => {
        dialog.close();
        dialog.remove();
        resolve(answer);
      };

      dialog.append(
        el("div", { class: "ff-modal__header" }, [
          el("div", { class: "ff-modal__icon" }, [icon("warning", 18)]),
          el("h2", { class: "ff-modal__title", text: options.title }),
          el(
            "button",
            {
              type: "button",
              class: "ff-btn ff-btn--ghost ff-btn--icon ff-btn--sm ff-modal__close",
              "aria-label": t("Cancel"),
              onclick: () => close(false),
            },
            [icon("close", 16)]
          ),
        ]),
        el("div", { class: "ff-modal__body" }, [
          options.body ? el("p", { text: options.body }) : null,
          options.detail ? el("p", { class: "ff-mt-2", text: options.detail }) : null,
        ]),
        el("div", { class: "ff-modal__footer" }, [
          el("button", {
            type: "button",
            class: "ff-btn",
            text: t("Cancel"),
            onclick: () => close(false),
          }),
          el("button", {
            type: "button",
            class: `ff-btn ${options.danger === false ? "ff-btn--primary" : "ff-btn--danger"}`,
            text: options.confirm || t("Delete"),
            onclick: () => close(true),
          }),
        ])
      );

      document.body.append(dialog);
      dialog.addEventListener("cancel", () => close(false));
      dialog.showModal();
      // Focus the safe option, not the destructive one: Enter on an unread
      // dialog should not be what deletes a row.
      dialog.querySelector(".ff-modal__footer .ff-btn").focus();
    });

  /* Posts to `action` with the page's CSRF pair. Used when the thing being
   * confirmed is a link whose href is a destructive endpoint: following it as a
   * GET would only reach the confirmation page, and asking twice for one
   * deletion is worse than asking once. */
  const postTo = (action) => {
    const form = el("form", { method: "post", action });
    form.append(
      el("input", {
        type: "hidden",
        name: document.body.dataset.ffCsrfField || "_csrf",
        value: document.body.dataset.ffCsrf || "",
      })
    );
    document.body.append(form);
    form.submit();
  };

  /* Any link or button carrying data-ff-confirm asks first. Declared in the
   * markup so a new destructive action gets the dialog by saying so, rather than
   * by being added to a list in here. */
  document.addEventListener("click", async (event) => {
    const trigger = event.target.closest("[data-ff-confirm]");
    if (!trigger || trigger.dataset.ffConfirmed === "1") return;

    event.preventDefault();
    const answer = await confirmDialog({
      title: trigger.dataset.ffConfirm,
      body: trigger.dataset.ffConfirmBody || "",
      detail: trigger.dataset.ffConfirmDetail || "",
      confirm: trigger.dataset.ffConfirmLabel,
    });
    if (!answer) return;

    if (trigger.dataset.ffMethod === "post" && trigger.href) {
      postTo(trigger.href);
      return;
    }

    // Re-dispatch rather than following manually: the element may be a link, a
    // submit button, or something with its own click handler.
    trigger.dataset.ffConfirmed = "1";
    trigger.click();
    delete trigger.dataset.ffConfirmed;
  });

  // --- Toasts --------------------------------------------------------------

  let toastRegion = null;

  const toast = (text, tone = "info") => {
    if (!toastRegion) {
      toastRegion = el("div", {
        class: "ff-toasts",
        role: "status",
        "aria-live": "polite",
      });
      document.body.append(toastRegion);
    }
    const marks = { success: "check-circle", danger: "close-circle", info: "info" };
    const node = el("div", { class: `ff-toast ff-toast--${tone}` }, [
      el("span", { class: "ff-toast__icon" }, [icon(marks[tone] || "info", 16)]),
      el("span", { class: "ff-toast__text", text }),
    ]);
    toastRegion.append(node);

    setTimeout(() => {
      node.dataset.ffLeaving = "true";
      setTimeout(() => node.remove(), 200);
    }, 4000);
  };

  window.FastFort.toast = toast;
  window.FastFort.confirm = confirmDialog;

  // =========================================================================
  // Command palette
  //
  // In an admin with thirty models the sidebar stops being navigation and
  // becomes a list to read. Typing three letters is faster than any of it.
  // =========================================================================

  let palette = null;

  const buildPalette = () => {
    const source = document.getElementById("ff-palette-data");
    if (!source) return null;
    const entries = JSON.parse(source.textContent);

    const input = el("input", {
      type: "text",
      autocomplete: "off",
      spellcheck: "false",
      placeholder: source.dataset.ffPlaceholder || t("Search"),
      "aria-label": source.dataset.ffPlaceholder || t("Search"),
    });
    const results = el("div", { class: "ff-palette__results", role: "listbox" });
    const dialog = el("dialog", { class: "ff-palette" }, [
      el("div", { class: "ff-palette__search" }, [icon("search", 18), input]),
      results,
      el("div", { class: "ff-palette__footer" }, [
        el("span", {}, [el("span", { class: "ff-kbd", text: "↑" }), el("span", { class: "ff-kbd", text: "↓" })]),
        el("span", { text: source.dataset.ffHintMove || "" }),
        el("span", { class: "ff-kbd", text: "↵" }),
        el("span", { text: source.dataset.ffHintOpen || "" }),
      ]),
    ]);
    document.body.append(dialog);

    let matches = [];
    let active = 0;

    const draw = () => {
      const term = input.value.trim().toLowerCase();
      matches = term
        ? entries.filter(
            (entry) =>
              entry.label.toLowerCase().includes(term) || entry.group.toLowerCase().includes(term)
          )
        : entries;

      results.replaceChildren();
      if (!matches.length) {
        results.append(el("div", { class: "ff-combo__note", text: t("NoResults") }));
        return;
      }

      let group = null;
      matches.forEach((entry, index) => {
        if (entry.group !== group) {
          group = entry.group;
          results.append(el("div", { class: "ff-palette__group", text: group }));
        }
        results.append(
          el(
            "a",
            {
              class: "ff-item",
              href: entry.url,
              role: "option",
              "data-ff-index": index,
              "aria-selected": String(index === active),
            },
            [
              el("span", { class: "ff-item__icon" }, [icon(entry.icon || "list", 16)]),
              el("span", { class: "ff-item__label", text: entry.label }),
              entry.hint ? el("span", { class: "ff-item__hint", text: entry.hint }) : null,
            ]
          )
        );
      });
      highlight(0);
    };

    const highlight = (index) => {
      const rows = [...results.querySelectorAll(".ff-item")];
      if (!rows.length) return;
      active = (index + rows.length) % rows.length;
      rows.forEach((row, position) => {
        row.dataset.ffActive = String(position === active);
        row.setAttribute("aria-selected", String(position === active));
      });
      rows[active].scrollIntoView({ block: "nearest" });
    };

    input.addEventListener("input", draw);
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        highlight(active + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        highlight(active - 1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        results.querySelectorAll(".ff-item")[active]?.click();
      }
    });

    dialog.addEventListener("click", (event) => {
      // A click on the backdrop lands on the dialog itself.
      if (event.target === dialog) dialog.close();
    });

    return { dialog, input, draw };
  };

  const openPalette = () => {
    palette = palette ?? buildPalette();
    if (!palette) return;
    palette.input.value = "";
    palette.draw();
    palette.dialog.showModal();
    palette.input.focus();
  };

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-ff-palette]")) {
      event.preventDefault();
      openPalette();
    }
  });

  // =========================================================================
  // Filter panel
  //
  // A drawer rather than a popover: it holds every filter a model declares, and
  // several of them are lists. Its contents are part of the toolbar form, so
  // Apply submits exactly what a plain form would.
  // =========================================================================

  const setFilters = (open) => {
    const panel = document.getElementById("ff-filter-panel");
    if (!panel) return;
    panel.hidden = !open;
    for (const button of document.querySelectorAll("[data-ff-filters]")) {
      button.setAttribute("aria-expanded", String(open));
    }
    if (open) {
      if (!document.querySelector(".ff-scrim--filters")) {
        const scrim = el("div", {
          class: "ff-scrim ff-scrim--filters",
          onclick: () => setFilters(false),
        });
        document.body.append(scrim);
      }
      panel.querySelector("input, select, button")?.focus();
    } else {
      document.querySelector(".ff-scrim--filters")?.remove();
    }
  };

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-ff-filters]")) {
      const panel = document.getElementById("ff-filter-panel");
      setFilters(Boolean(panel?.hidden));
    } else if (event.target.closest("[data-ff-filters-close]")) {
      setFilters(false);
    }
  });

  /* Date presets write into the two bounds rather than being a filter of their
   * own, so the server sees one thing -- a range -- however it was chosen, and
   * the boxes always show what is actually being filtered. */
  const markPreset = (group, active) => {
    for (const button of group.querySelectorAll("[data-ff-from]")) {
      button.setAttribute("aria-pressed", String(button === active));
    }
  };

  document.addEventListener("click", (event) => {
    const preset = event.target.closest("[data-ff-from]");
    if (!preset) return;

    const group = preset.closest(".ff-filter-group");
    const [from, to] = group.querySelectorAll(".ff-range-panel input");
    from.value = preset.dataset.ffFrom;
    to.value = preset.dataset.ffTo;
    markPreset(group, preset);
    from.dispatchEvent(new Event("change", { bubbles: true }));
  });

  // Typing a bound by hand is a range the presets do not describe, so none of
  // them should keep claiming to be the answer.
  document.addEventListener("change", (event) => {
    const bound = event.target.closest(".ff-range-panel input");
    if (bound) markPreset(bound.closest(".ff-filter-group"), null);
  });

  // =========================================================================
  // Listing: opening a row
  // =========================================================================

  /* A click anywhere in a row opens it. Everything that is already interactive
   * is excluded, so the checkbox still selects, the delete button still asks,
   * and a link in a cell still goes where it says. A drag that happens to end
   * inside a row is a text selection, not a click on it. */
  document.addEventListener("click", (event) => {
    const row = event.target.closest("[data-ff-row-url]");
    if (!row) return;
    if (event.target.closest("a, button, input, label, select, textarea")) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    if (window.getSelection()?.toString()) return;

    window.location.assign(row.dataset.ffRowUrl);
  });

  // =========================================================================
  // Listing: row selection and bulk actions
  // =========================================================================

  /* Django's changelist actions, which is the feature people miss first: without
   * it, deleting fifty rows is fifty confirmations. */
  enhancers.push((scope) => {
    const table = scope.querySelector?.("[data-ff-selectable]") ?? null;
    const container = table || (scope.matches?.("[data-ff-selectable]") ? scope : null);
    if (!container || !once(container, "ffSelectReady")) return;

    const bar = document.getElementById("ff-bulkbar");
    const all = container.querySelector("[data-ff-select-all]");
    const boxes = () => [...container.querySelectorAll("[data-ff-select-row]")];

    const sync = () => {
      const rows = boxes();
      const chosen = rows.filter((box) => box.checked);

      for (const box of rows) {
        box.closest("tr").dataset.ffSelected = String(box.checked);
      }
      if (all) {
        all.checked = chosen.length > 0 && chosen.length === rows.length;
        all.indeterminate = chosen.length > 0 && chosen.length < rows.length;
      }
      if (bar) {
        bar.hidden = chosen.length === 0;
        const count = bar.querySelector("[data-ff-count]");
        if (count) {
          count.textContent = `${chosen.length} ${t("Selected")}`;
        }
        // The action form posts the keys, so they have to be in it.
        const holder = bar.querySelector("[data-ff-keys]");
        if (holder) {
          holder.replaceChildren(
            ...chosen.map((box) => el("input", { type: "hidden", name: "keys", value: box.value }))
          );
        }
      }
    };

    all?.addEventListener("change", () => {
      for (const box of boxes()) box.checked = all.checked;
      sync();
    });

    container.addEventListener("change", (event) => {
      if (event.target.matches("[data-ff-select-row]")) sync();
    });

    // Shift-click selects a range, the way every file manager does.
    let anchor = null;
    container.addEventListener("click", (event) => {
      const box = event.target.closest("[data-ff-select-row]");
      if (!box) return;
      const rows = boxes();
      if (event.shiftKey && anchor !== null) {
        const from = rows.indexOf(box);
        const [start, end] = [Math.min(from, anchor), Math.max(from, anchor)];
        for (let index = start; index <= end; index += 1) rows[index].checked = box.checked;
        sync();
      }
      anchor = rows.indexOf(box);
    });

    sync();
  });

  // =========================================================================
  // Live list updates
  //
  // Written here rather than by vendoring HTMX: the list view needs one
  // behaviour -- fetch a fragment and swap it -- and that is about sixty lines.
  // Pulling in a general-purpose library for it would add 14 KB, a dependency to
  // keep audited, and a second mental model for how the page updates.
  // =========================================================================

  const startLiveList = () => {
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
      form.classList.add("ff-loading");

      try {
        const response = await fetch(url, {
          headers: { "X-FastFort-Partial": "results" },
          credentials: "same-origin",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(String(response.status));
        target.innerHTML = await response.text();
        // The fragment brings its own controls -- row checkboxes, action menus --
        // and they are inert until they are upgraded like everything else.
        enhance(target);
        document.getElementById("ff-bulkbar")?.setAttribute("hidden", "");
        if (push) window.history.pushState({ ff: true }, "", url);
      } catch (error) {
        if (error.name === "AbortError") return;
        // Fall back to a full navigation. A list that silently stops responding
        // is worse than one that reloads.
        window.location.assign(url);
        return;
      } finally {
        if (inFlight === controller) {
          inFlight = null;
          delete target.dataset.ffLoading;
          target.setAttribute("aria-busy", "false");
          form.classList.remove("ff-loading");
        }
      }
    };

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      load(queryFrom({ p: null }));
    });

    // A changed dropdown, date bound or filter checkbox applies immediately:
    // making someone press Apply after picking from a select is a step with no
    // purpose, and the drawer stays open so the next tick refines the same
    // answer. Apply is still there, and is what submits without script.
    form.addEventListener("change", (event) => {
      if (
        event.target.matches("select, input[type='date'], input[type='datetime-local']") ||
        event.target.closest("#ff-filter-panel")
      ) {
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
      if (link.hasAttribute("data-ff-confirm")) return; // a delete, not a view change

      const url = new URL(link.href, window.location.origin);
      if (url.pathname !== base) return; // a row link leaves the list

      event.preventDefault();
      load(url.searchParams);
    });

    // Back and forward must move through the list, not out of it.
    window.addEventListener("popstate", () => {
      load(new URLSearchParams(window.location.search), { push: false });
    });
  };

  // =========================================================================
  // Start
  // =========================================================================

  enhance(document);
  startLiveList();
  syncThemeControls();
})();
