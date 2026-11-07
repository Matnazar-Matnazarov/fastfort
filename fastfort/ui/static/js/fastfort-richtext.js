/* The glue between a rich-text editor the project loads and the textareas
 * FastFort marks `data-ff-richtext`.
 *
 * `UISettings.richtext_url` has always named the editor's script, and the CSP
 * admits that one origin -- but an editor's script only *defines* the editor.
 * Something has to call `ClassicEditor.create(textarea)`, and under a policy
 * with no inline script that something could not be a line in a template. So
 * every project that wanted CKEditor had to host a second file of its own just
 * to say "start". This is that file, served from the admin's own origin, run
 * only when `UISettings.richtext_editor` names which editor to start.
 *
 * Deferred after the editor's script, so the global it looks for exists. The
 * textarea stays the element that submits: CKEditor writes its data back into
 * it on submit, TinyMCE through `save()`, and with neither present -- a CDN
 * that did not answer -- the page is the plain textarea it always was.
 */
(() => {
  "use strict";

  const script = document.currentScript;
  const editor = script ? script.dataset.editor : "";

  // The toolbar an admin's long-text field needs, and no more: a page builder
  // is a different product. Every item is a plugin in CKEditor's free
  // distribution, so the GPL licence key below is enough to run it.
  const CKEDITOR_PLUGINS = [
    "Essentials",
    "Paragraph",
    "Heading",
    "Bold",
    "Italic",
    "Underline",
    "Strikethrough",
    "Code",
    "Link",
    "List",
    "BlockQuote",
    "CodeBlock",
    "Table",
    "TableToolbar",
    "HorizontalLine",
    "RemoveFormat",
    "Autoformat",
  ];

  const CKEDITOR_TOOLBAR = [
    "heading",
    "|",
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "code",
    "|",
    "link",
    "bulletedList",
    "numberedList",
    "blockQuote",
    "codeBlock",
    "insertTable",
    "horizontalLine",
    "|",
    "removeFormat",
    "|",
    "undo",
    "redo",
  ];

  const ckeditor = (area) => {
    const kit = window.CKEDITOR;
    // 42 and later ship one UMD bundle exposing every plugin on `CKEDITOR`;
    // 41 and earlier ship prebuilt editors as a bare `ClassicEditor`. Both
    // are still on CDNs, and a project should not have to know which it has.
    if (kit && kit.ClassicEditor) {
      const plugins = CKEDITOR_PLUGINS.map((name) => kit[name]).filter(Boolean);
      return kit.ClassicEditor.create(area, {
        licenseKey: script.dataset.licenseKey || "GPL",
        plugins,
        // An item whose plugin a trimmed build lacks is skipped by CKEditor
        // with a console warning, not an error, so one list serves every build.
        toolbar: CKEDITOR_TOOLBAR,
        language: document.documentElement.lang || "en",
        table: { contentToolbar: ["tableColumn", "tableRow", "mergeTableCells"] },
      });
    }
    if (window.ClassicEditor) return window.ClassicEditor.create(area);
    return null;
  };

  // `data-ff-theme` is absent when the theme follows the system, so the media
  // query is the answer then -- read once, as TinyMCE picks a skin only once.
  const dark = () => {
    const chosen = document.documentElement.dataset.ffTheme;
    if (chosen) return chosen === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  };

  const tinymce = (area) => {
    if (!window.tinymce) return null;
    return window.tinymce.init({
      target: area,
      license_key: script.dataset.licenseKey || "gpl",
      menubar: false,
      promotion: false,
      // Follows the admin's theme rather than drawing a white box on a dark page.
      skin: dark() ? "oxide-dark" : "oxide",
      content_css: dark() ? "dark" : "default",
    });
  };

  const START = { ckeditor5: ckeditor, tinymce };

  const boot = () => {
    const start = START[editor];
    if (!start) return;
    for (const area of document.querySelectorAll("textarea[data-ff-richtext]")) {
      if (area.dataset.ffRichtextReady === "1") continue;
      area.dataset.ffRichtextReady = "1";
      try {
        const pending = start(area);
        // An editor that fails to start leaves the textarea where it was, in
        // the form, submitting -- which is the whole fallback. Logged rather
        // than shown: the person filling the form can still fill it.
        if (pending && typeof pending.catch === "function") {
          pending.catch((error) => console.error("FastFort: rich-text editor failed", error));
        }
      } catch (error) {
        console.error("FastFort: rich-text editor failed", error);
      }
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
