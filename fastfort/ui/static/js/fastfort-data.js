/* Editors for the structured column types: arrays, hstore, JSON, addresses.
 *
 * Its own file, requested only by a page that renders one of them, for the
 * same reason as `fastfort-geo.js`: most admin pages have no array and no
 * hstore, and a size budget spent on every page is spent badly. `script-src
 * 'self'` already covers a second file from the same origin, so nothing about
 * the policy changes.
 *
 * Every widget here upgrades a control that already works. The comma-separated
 * box, the `key: value` textarea and the JSON textarea are what the server
 * renders and what `admin/values.py` parses; with scripting off they are the
 * field, and each of these does no more than make the same value easier to get
 * right. Nothing here is the thing that submits -- the original control stays
 * in the form and keeps its name, and every editor writes back into it.
 */

(() => {
  "use strict";

  const kit = window.FastFort;
  // See the same guard in `fastfort-geo.js`: a project that overrides
  // `base.html` and reorders the tags should lose one widget, not the page.
  if (!kit) return;

  const { el, icon, t, once, register } = kit;

  /* Write into the control the form actually submits, and say so.
   *
   * The `change` event is what the unsaved-changes guard and anything else
   * bound to the field are listening for. An editor that only sets `.value`
   * leaves a form that looks untouched and warns about nothing when the page
   * is closed with edits in it. */
  const commit = (control, value) => {
    if (control.value === value) return;
    control.value = value;
    control.dispatchEvent(new Event("change", { bubbles: true }));
  };

  // =========================================================================
  // Tag input, for an ARRAY column
  //
  // The server renders "alpha, beta, gamma" and parses the same back. That is
  // a perfectly good control right up to the moment a value contains a comma
  // or there are fifteen of them, at which point it is a wall of text with no
  // way to see where one entry ends. Chips make the entries countable and
  // removable; the box underneath still holds exactly what it always did.
  // =========================================================================

  /* Whether one entry is the type the column's items actually are.
   *
   * `data-ff-item-type` comes from `FieldSpec.item`, which the type registry
   * fills in from `ARRAY(Integer)` and friends. Without it the server was the
   * first thing to notice "banana" in an integer array, one round trip later. */
  const itemIsValid = (text, kind) => {
    if (kind === "integer" || kind === "bigint") return /^-?\d+$/.test(text);
    if (kind === "float" || kind === "decimal" || kind === "money") {
      return Number.isFinite(Number(text));
    }
    if (kind === "uuid") {
      return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(text);
    }
    return true;
  };

  const splitEntries = (value) =>
    String(value || "")
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);

  class TagInput {
    constructor(input) {
      this.input = input;
      this.kind = input.dataset.ffItemType || "string";
      this.entries = splitEntries(input.value);

      this.box = el("input", {
        type: "text",
        class: "ff-tags__entry",
        // Not `required`: the chips are the value, and a browser refusing to
        // submit because the *typing* box is empty would block every form
        // whose tags are already entered.
        placeholder: input.placeholder || t("Add"),
        "aria-label": t("Add"),
      });
      this.chips = el("div", { class: "ff-tags__chips" });
      this.element = el("div", { class: "ff-tags ff-js-only" }, [this.chips, this.box]);

      input.after(this.element);
      // The original stays in the form and stays the thing that submits; it is
      // only hidden, so no value and no `name` moves anywhere.
      input.classList.add("ff-no-js");
      this.bind();
      this.render();
    }

    bind() {
      this.box.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === ",") {
          // Enter inside a form submits it, and a comma is the separator, not
          // a character anyone means to put inside one entry here.
          event.preventDefault();
          this.add(this.box.value);
          return;
        }
        // Backspace on an empty box takes the previous chip, which is what
        // every tag control does and what a hand reaches for without looking.
        if (event.key === "Backspace" && !this.box.value && this.entries.length) {
          this.entries.pop();
          this.flush();
        }
      });

      // Leaving the field with something typed keeps it. Losing an entry
      // because it was never confirmed with Enter is the commonest complaint
      // about every control of this shape.
      this.box.addEventListener("blur", () => this.add(this.box.value));

      // Pasting a comma-separated list makes as many chips as it has entries.
      this.box.addEventListener("paste", (event) => {
        const text = event.clipboardData?.getData("text") || "";
        if (!text.includes(",")) return;
        event.preventDefault();
        for (const entry of splitEntries(text)) this.add(entry);
      });

      this.chips.addEventListener("click", (event) => {
        const button = event.target.closest("[data-ff-tag-remove]");
        if (!button) return;
        event.preventDefault();
        this.entries.splice(Number(button.dataset.ffTagRemove), 1);
        this.flush();
      });
    }

    add(text) {
      const entry = text.trim();
      this.box.value = "";
      if (!entry) return;
      // A duplicate is almost always a slip; the array can hold one, but
      // adding it by typing the same word twice is not how anyone means to.
      if (!this.entries.includes(entry)) this.entries.push(entry);
      this.flush();
    }

    flush() {
      commit(this.input, this.entries.join(", "));
      this.render();
    }

    render() {
      this.chips.replaceChildren(
        ...this.entries.map((entry, index) => {
          const remove = el("button", {
            type: "button",
            class: "ff-chip__remove",
            "data-ff-tag-remove": index,
            "aria-label": `${t("Remove")}: ${entry}`,
          });
          remove.append(icon("close", 12));
          const chip = el("span", { class: "ff-chip", text: entry });
          // Marked rather than refused: the server is still the authority, and
          // a control that silently drops what was typed is worse than one
          // that shows it is wrong.
          if (!itemIsValid(entry, this.kind)) chip.dataset.ffInvalid = "true";
          chip.append(remove);
          return chip;
        })
      );
    }
  }

  register((scope) => {
    for (const input of scope.querySelectorAll("input[data-ff-tags]")) {
      if (!once(input, "ffTagsReady")) continue;
      new TagInput(input);
    }
  });

  // =========================================================================
  // Key/value editor, for an HSTORE column
  //
  // The textarea holds "key: value" per line, which is what `_parse_hstore`
  // reads. Paired boxes make it obvious which half is which, and stop the
  // commonest mistake with the raw form: a value containing a colon, typed
  // into a line whose first colon is then the separator.
  // =========================================================================

  const readPairs = (text) =>
    String(text || "")
      .split("\n")
      .map((line) => {
        const at = line.indexOf(":");
        if (at === -1) return line.trim() ? [line.trim(), ""] : null;
        return [line.slice(0, at).trim(), line.slice(at + 1).trim()];
      })
      .filter(Boolean);

  const writePairs = (pairs) =>
    pairs
      .filter(([key]) => key)
      .map(([key, value]) => `${key}: ${value}`)
      .join("\n");

  class KeyValue {
    constructor(area) {
      this.area = area;
      this.pairs = readPairs(area.value);
      this.rows = el("div", { class: "ff-kv__rows" });

      const add = el("button", { type: "button", class: "ff-btn ff-btn--ghost ff-btn--sm" }, [
        icon("plus", 14),
      ]);
      add.append(document.createTextNode(` ${t("Add")}`));
      add.addEventListener("click", (event) => {
        event.preventDefault();
        this.pairs.push(["", ""]);
        this.render();
        // The key box of the row that was just added, because adding a row and
        // then having to click into it is two gestures for one intention.
        this.rows.lastElementChild?.querySelector("input")?.focus();
      });

      this.element = el("div", { class: "ff-kv ff-js-only" }, [this.rows, add]);
      area.after(this.element);
      area.classList.add("ff-no-js");

      this.rows.addEventListener("input", () => this.flush());
      this.rows.addEventListener("click", (event) => {
        const button = event.target.closest("[data-ff-kv-remove]");
        if (!button) return;
        event.preventDefault();
        this.pairs.splice(Number(button.dataset.ffKvRemove), 1);
        this.render();
        this.flush();
      });

      this.render();
    }

    /* Read back off the boxes rather than off `this.pairs`.
     *
     * The rows are the state while someone is typing in them; keeping a
     * parallel copy in sync on every keystroke is the version of this that had
     * a row lag one character behind what was on screen. */
    flush() {
      this.pairs = [...this.rows.children].map((row) => {
        const [key, value] = row.querySelectorAll("input");
        return [key.value.trim(), value.value];
      });
      commit(this.area, writePairs(this.pairs));
    }

    render() {
      this.rows.replaceChildren(
        ...this.pairs.map(([key, value], index) => {
          const remove = el("button", {
            type: "button",
            class: "ff-kv__remove",
            "data-ff-kv-remove": index,
            "aria-label": t("Remove"),
          });
          remove.append(icon("close", 13));
          return el("div", { class: "ff-kv__row" }, [
            el("input", { type: "text", class: "ff-input", value: key, "aria-label": t("Key") }),
            el("input", {
              type: "text",
              class: "ff-input",
              value,
              "aria-label": t("Value"),
            }),
            remove,
          ]);
        })
      );
    }
  }

  register((scope) => {
    for (const area of scope.querySelectorAll("textarea[data-ff-keyvalue]")) {
      if (!once(area, "ffKeyValueReady")) continue;
      new KeyValue(area);
    }
  });

  // =========================================================================
  // JSON
  //
  // The textarea stays the editor -- a tree view that cannot express every
  // document is a trap, and this one has to hold whatever the column holds.
  // What it gains is the two things a person actually wants from a JSON box:
  // being told immediately that it will not parse, and having it laid out.
  // =========================================================================

  register((scope) => {
    for (const area of scope.querySelectorAll("textarea[data-ff-json]")) {
      if (!once(area, "ffJsonReady")) continue;

      const status = el("span", { class: "ff-json__status" });
      const format = el("button", { type: "button", class: "ff-btn ff-btn--ghost ff-btn--sm" }, [
        icon("sliders", 14),
      ]);
      format.append(document.createTextNode(` ${t("Format")}`));

      const check = () => {
        const text = area.value.trim();
        // Empty is not invalid: a nullable JSON column is allowed to be empty,
        // and colouring the box red for it is telling someone off for the
        // default state of their own field.
        if (!text) {
          delete area.dataset.ffInvalid;
          status.textContent = "";
          return null;
        }
        try {
          const parsed = JSON.parse(text);
          delete area.dataset.ffInvalid;
          status.textContent = "";
          return parsed;
        } catch (error) {
          area.dataset.ffInvalid = "true";
          // The parser's own message names the position, which is the only
          // part of a JSON error anybody can act on.
          status.textContent = error.message;
          return null;
        }
      };

      format.addEventListener("click", (event) => {
        event.preventDefault();
        const parsed = check();
        if (parsed === null) return;
        commit(area, JSON.stringify(parsed, null, 2));
      });

      area.addEventListener("input", check);
      area.after(el("div", { class: "ff-json__bar ff-js-only" }, [format, status]));
      check();
    }
  });

  // =========================================================================
  // Addresses
  //
  // `pattern` already stops the form submitting a malformed address, but only
  // at submit time and with the browser's own wording. This marks the box as
  // soon as the field is left, next to the other field-level errors on the
  // page, which is where someone is already looking.
  // =========================================================================

  const looksLikeAddress = (text) => {
    const [address, prefix, ...rest] = text.split("/");
    if (rest.length) return false;
    if (prefix !== undefined && !/^\d{1,3}$/.test(prefix)) return false;
    // Four dotted decimals, or anything with a colon in it, which is the only
    // cheap test for IPv6 worth running in a browser -- `ipaddress` on the
    // server is what actually decides.
    if (address.includes(":")) return /^[0-9A-Fa-f:.]+$/.test(address);
    const octets = address.split(".");
    return (
      octets.length === 4 &&
      octets.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
    );
  };

  register((scope) => {
    for (const input of scope.querySelectorAll("input[data-ff-inet]")) {
      if (!once(input, "ffInetReady")) continue;
      input.addEventListener("blur", () => {
        const text = input.value.trim();
        if (text && !looksLikeAddress(text)) {
          input.dataset.ffInvalid = "true";
          input.setAttribute("aria-invalid", "true");
        } else {
          delete input.dataset.ffInvalid;
          input.removeAttribute("aria-invalid");
        }
      });
    }
  });

  // A MAC address is six bytes however it is spelled, so this normalises to the
  // colon form on the way out rather than complaining about the other three.
  // `_parse_macaddr` accepts all of them; agreeing with it is friendlier than
  // making someone retype a form the server would have taken.
  register((scope) => {
    for (const input of scope.querySelectorAll("input[data-ff-mac]")) {
      if (!once(input, "ffMacReady")) continue;
      input.addEventListener("blur", () => {
        const hex = input.value.replace(/[^0-9A-Fa-f]/g, "").toLowerCase();
        if (hex.length !== 12) return;
        commit(input, hex.match(/../g).join(":"));
      });
    }
  });
})();
