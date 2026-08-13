/* The two preferences that have to be applied before the first paint.
 *
 * Deliberately its own file, loaded from <head> without `defer`, so it runs
 * while the document is still being parsed and before anything is drawn. The
 * main script is deferred -- correctly, it should not block the page -- but that
 * means it runs *after* the first paint, so anything it sets is a visible
 * change: the admin flashed dark before turning light, and the sidebar opened
 * before snapping shut, on every single navigation.
 *
 * Both values are written onto <html>, which is the only element that exists at
 * this point in the parse. The stylesheet therefore keys the collapsed sidebar
 * off the root rather than off `.ff-app`, which has not been parsed yet.
 *
 * A separate file rather than an inline <script> because the admin's CSP is
 * `script-src 'self'` with no 'unsafe-inline' and no nonce. Keeping it that way
 * is worth one small cached request.
 */

(() => {
  "use strict";

  const root = document.documentElement;

  /* "Scripting is on", declared here rather than by the main bundle.
   *
   * The stylesheet keys two opposite rules off this: `.ff-js-only` is hidden
   * without it, and `.ff-no-js` is hidden with it. Set from the deferred bundle
   * -- which runs after the browser has already painted -- both were wrong for
   * the first frame of every single navigation. The theme switch, the settings
   * button and the command palette blinked into existence a moment after the
   * page appeared, and every native <select> that is about to be replaced was
   * drawn in the operating system's own styling first and then swapped.
   *
   * This file is the right place for it because this file is the proof: it is
   * running, so scripting is on, and it runs before the first paint. */
  root.dataset.ffJs = "1";

  /* localStorage throws in private mode in some browsers, and a preference is
   * never worth breaking the page over. */
  const read = (key) => {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  };

  /* "system" is the absence of the attribute, which is what the stylesheet's
   * media query keys off; writing data-ff-theme="system" would match neither
   * branch. An unrecognised value leaves whatever the server rendered. */
  const theme = read("ff:theme");
  if (theme === "light" || theme === "dark") root.dataset.ffTheme = theme;
  else if (theme === "system") delete root.dataset.ffTheme;

  if (read("ff:sidebar-collapsed") === "true") root.dataset.ffCollapsed = "true";

  /* The accent hue and the density, for the same reason: applied late they are a
   * visible repaint of the whole page. Both are validated here rather than
   * trusted -- they go into a `style` attribute, and local storage is writable
   * by anything else running on the origin. */
  const hue = Number(read("ff:accent"));
  if (Number.isFinite(hue) && hue >= 0 && hue <= 360) {
    root.style.setProperty("--ff-h", String(hue));
  }

  if (read("ff:density") === "compact") root.dataset.ffDensity = "compact";
  else if (read("ff:density") === "comfortable") root.dataset.ffDensity = "comfortable";

  if (read("ff:primary") === "accent") root.dataset.ffPrimary = "accent";

  /* The current model, kept on screen in a nav that scrolls by itself.
   *
   * `.ff-nav` is its own scroll container, and a scroll container starts every
   * navigation at zero. In an admin whose models run past the fold that meant
   * the page marked "you are here" on a row nobody could see: open something
   * near the bottom of the sidebar, or reload while you are already there, and
   * the highlight was below the visible band every time.
   *
   * Nothing is remembered between pages on purpose. Where the reader last left
   * the *scrollbar* is a worse answer than where the reader actually is, and it
   * would need a listener and a storage key to be wrong with.
   *
   * In this file rather than the deferred bundle for this file's whole reason to
   * exist: run after the first paint, this is a sidebar that visibly jumps once
   * the page is already on screen. The nav does not exist while <head> is being
   * parsed, so the work waits on a MutationObserver and runs the moment the
   * element *after* it appears -- the sidebar's footer, whose presence is proof
   * the parser is past `</nav>` and the items have their real heights. */
  const centreCurrentNavItem = () => {
    const nav = document.querySelector(".ff-sidebar .ff-nav");
    const current = nav && nav.querySelector('[aria-current="page"]');
    if (!current) return;

    /* Rects rather than `offsetTop`: offsetTop is measured from the nearest
     * *positioned* ancestor, which is the sidebar and not the nav, so it folds
     * the brand header's height into the answer and centres on the wrong row. */
    const navBox = nav.getBoundingClientRect();
    const itemBox = current.getBoundingClientRect();
    // One row of slack at each edge, so an item that is technically visible but
    // flush against the boundary still reads as part of the list.
    if (itemBox.top >= navBox.top + itemBox.height && itemBox.bottom <= navBox.bottom - itemBox.height) {
      return;
    }
    const offset = itemBox.top - navBox.top + nav.scrollTop;
    nav.scrollTop = offset - (nav.clientHeight - itemBox.height) / 2;
  };

  // The footer, not the nav itself: a nav is in the document as soon as its
  // opening tag is parsed, when it is still empty and every measurement is zero.
  const navReady = () => Boolean(document.querySelector(".ff-sidebar__footer"));

  if (navReady()) {
    centreCurrentNavItem();
  } else {
    const observer = new MutationObserver(() => {
      if (!navReady()) return;
      observer.disconnect();
      centreCurrentNavItem();
    });
    observer.observe(root, { childList: true, subtree: true });

    // A popup renders no sidebar at all, so without this the observer would watch
    // the whole document forever for a footer that is never coming.
    document.addEventListener("DOMContentLoaded", () => observer.disconnect(), { once: true });
  }
})();
