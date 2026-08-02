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
})();
