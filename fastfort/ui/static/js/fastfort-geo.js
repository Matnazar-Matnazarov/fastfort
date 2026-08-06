/* The map, and the editor for every geometry a spatial column can hold.
 *
 * Its own file, requested only by a page that actually renders a geometry
 * field. This is about half the weight of the everyday bundle and the great
 * majority of admin pages have no spatial column at all, so loading it with
 * `fastfort.js` meant every list view in every project paying for a polygon
 * editor it would never open. `admin/site.py` decides per page; the CSP needs
 * no change, because `script-src 'self'` covers a second file from the same
 * origin exactly as it covers the first.
 *
 * Everything it borrows from the main bundle comes through `window.FastFort`,
 * which that file publishes: there is no module system here and no build step
 * to add one. `register()` both enrols the pass and runs it once, so this file
 * works whether it is evaluated before or after the first `enhance(document)`.
 *
 * A pair of coordinates is a number nobody can check. "51.5074, -0.1278" is
 * either the right place or a transposed pair a thousand miles away, and the
 * only way to tell is to look at it. That was true when this only drew points
 * and it is more true for a polygon, where the text form is a hundred numbers
 * in a row.
 *
 * Tiles are images from whatever server `UISettings.map_tile_url` names, so the
 * map only exists when a project configured one -- the admin's CSP starts at
 * `default-src 'none'` and that setting is what adds the host to it. With no
 * URL, or no script, the field is the text control it always was.
 *
 * Web Mercator, the same projection every tile service uses, so the arithmetic
 * is the standard one and tiles from any of them line up.
 */

(() => {
  "use strict";

  const kit = window.FastFort;
  // The main bundle is a hard prerequisite and `base.html` orders the two tags,
  // but a project that overrides that template could get it wrong, and a
  // ReferenceError here would take the whole page's scripting down rather than
  // just the map.
  if (!kit) return;

  const { el, icon, t, once, register } = kit;

  const TILE = 256;
  //: The deepest level a tile server has pictures for. Standard OpenStreetMap
  //: tiles stop at 19; asking for 20 returns a 404, and a failed tile is a
  //: blank square, so this is a ceiling on *requests* rather than on the view.
  const MAX_TILE_ZOOM = 19;
  //: How far the view itself goes. Past `MAX_TILE_ZOOM` the last level is
  //: scaled up rather than refetched -- blurrier, which is what every map does
  //: past its own imagery, and far better than a + button that does nothing.
  const MAX_ZOOM = 21;
  const MIN_ZOOM = 1;
  /* The projection is undefined at the poles and grows without bound towards
   * them; every slippy map clamps at the latitude that makes the world square. */
  const MAX_LAT = 85.05112878;

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

  /* Longitude back into -180..180 after panning around the world. Without it a
   * drag eastward keeps counting -- 190, 250, 540 -- and the box under the map,
   * which is the thing that actually submits, fills up with a coordinate no
   * database will take. */
  const wrapLng = (lng) => ((((lng + 180) % 360) + 360) % 360) - 180;

  /* The offset of the same longitude in whichever copy of the world the view is
   * currently over. Panning is unbounded, so the point being marked may be a
   * whole world's width to the left or right of where it was drawn. */
  const nearestCopy = (offset, span) => offset - span * Math.round(offset / span);

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

  // -------------------------------------------------------------------------
  // WKT, the other half of `admin/geo.py`
  //
  // A POINT column renders as "lat, lng" and everything else renders as EWKT,
  // because that is the only form the server can read back unchanged. So the
  // editor has to speak both, and it has to write the shape `geo.parse` accepts
  // -- a control that produces text its own server rejects is worse than a
  // control that does nothing.
  //
  // Coordinates are held here the way GeoJSON and WKT both hold them,
  // [longitude, latitude]. The lat-first order belongs to the point box alone,
  // where a person types it; every conversion between the two is in one of the
  // four functions below rather than scattered through the drawing code, which
  // is how the transposition bug this file exists to prevent gets prevented.
  // -------------------------------------------------------------------------

  const KINDS = {
    POINT: "Point",
    LINESTRING: "LineString",
    POLYGON: "Polygon",
    MULTIPOINT: "MultiPoint",
    MULTILINESTRING: "MultiLineString",
    MULTIPOLYGON: "MultiPolygon",
    GEOMETRYCOLLECTION: "GeometryCollection",
  };

  /* Split on commas that are not inside brackets, so a POLYGON's rings survive
   * being separated from each other. */
  const splitTop = (text) => {
    const parts = [];
    let depth = 0;
    let start = 0;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      if (ch === "(") depth += 1;
      else if (ch === ")") depth -= 1;
      else if (ch === "," && depth === 0) {
        parts.push(text.slice(start, i));
        start = i + 1;
      }
    }
    parts.push(text.slice(start));
    return parts.map((part) => part.trim()).filter(Boolean);
  };

  const unwrap = (text) => {
    const trimmed = text.trim();
    return trimmed.startsWith("(") && trimmed.endsWith(")")
      ? trimmed.slice(1, -1).trim()
      : trimmed;
  };

  /* One "lng lat" pair. Z and M ordinates are read and dropped: the editor is
   * two-dimensional, and silently keeping a third number that nothing can move
   * would write back a coordinate the person never saw. */
  const readCoord = (text) => {
    const numbers = text.trim().split(/\s+/).map(Number);
    return numbers.length >= 2 && numbers.every(Number.isFinite)
      ? [numbers[0], numbers[1]]
      : null;
  };

  const readRing = (text) => splitTop(unwrap(text)).map(readCoord).filter(Boolean);

  const readWkt = (raw) => {
    // The SRID prefix is the column's, not the shape's; it is put back on write.
    const text = String(raw || "").replace(/^\s*SRID=\d+\s*;/i, "").trim();
    const match = /^([A-Za-z]+)\s*(?:Z|M|ZM)?\s*\((.*)\)\s*$/s.exec(text);
    if (!match) return null;
    const kind = KINDS[match[1].toUpperCase()];
    const body = match[2];
    if (!kind) return null;

    if (kind === "Point") {
      const point = readCoord(body);
      return point && { type: kind, coordinates: point };
    }
    if (kind === "LineString") return { type: kind, coordinates: readRing(body) };
    if (kind === "MultiPoint") return { type: kind, coordinates: readRing(body) };
    if (kind === "Polygon" || kind === "MultiLineString") {
      return { type: kind, coordinates: splitTop(body).map(readRing) };
    }
    if (kind === "MultiPolygon") {
      return {
        type: kind,
        coordinates: splitTop(body).map((polygon) => splitTop(unwrap(polygon)).map(readRing)),
      };
    }
    // A collection is drawn but never edited, so its members only need reading.
    return { type: kind, geometries: splitTop(body).map(readWkt).filter(Boolean) };
  };

  /* Nine decimals then trimmed, matching `_num` in `admin/geo.py` exactly, so a
   * shape that is opened and saved without being touched round-trips to the
   * same bytes rather than churning the column on every visit. */
  const num = (value) => {
    const text = value.toFixed(9).replace(/0+$/, "").replace(/\.$/, "");
    return text || "0";
  };

  const coordText = (point) => `${num(point[0])} ${num(point[1])}`;
  const ringText = (ring) => ring.map(coordText).join(", ");

  const wktBody = (shape) => {
    const kind = shape.type;
    if (kind === "Point") return `POINT(${coordText(shape.coordinates)})`;
    if (kind === "LineString") return `LINESTRING(${ringText(shape.coordinates)})`;
    if (kind === "Polygon") {
      return `POLYGON(${shape.coordinates.map((ring) => `(${ringText(ring)})`).join(", ")})`;
    }
    if (kind === "MultiPoint") {
      return `MULTIPOINT(${shape.coordinates.map((p) => `(${coordText(p)})`).join(", ")})`;
    }
    if (kind === "MultiLineString") {
      return `MULTILINESTRING(${shape.coordinates
        .map((line) => `(${ringText(line)})`)
        .join(", ")})`;
    }
    if (kind === "MultiPolygon") {
      return `MULTIPOLYGON(${shape.coordinates
        .map((polygon) => `(${polygon.map((ring) => `(${ringText(ring)})`).join(", ")})`)
        .join(", ")})`;
    }
    return `GEOMETRYCOLLECTION(${shape.geometries.map(wktBody).join(", ")})`;
  };

  /* Every [lng, lat] pair in a shape, whatever its nesting, as one flat list --
   * enough to frame the view and to draw a handle per vertex without each kind
   * needing its own walk. */
  const everyPoint = (shape) => {
    if (!shape) return [];
    if (shape.type === "GeometryCollection") return shape.geometries.flatMap(everyPoint);
    const flatten = (value) =>
      typeof value[0] === "number" ? [value] : value.flatMap(flatten);
    return shape.coordinates.length ? flatten(shape.coordinates) : [];
  };

  /* The middle of a set of points, used to frame a shape the first time it is
   * drawn. The mean of the corners rather than of every vertex: a coastline
   * with two thousand points along one edge and four along the other would
   * otherwise centre the view on the crowded edge. */
  const centreOf = (points) => {
    const lngs = points.map((point) => point[0]);
    const lats = points.map((point) => point[1]);
    return {
      lng: (Math.min(...lngs) + Math.max(...lngs)) / 2,
      lat: (Math.min(...lats) + Math.max(...lats)) / 2,
    };
  };

  /* Every line the shape draws, flattened, each marked as an open path or a
   * closed one. A polygon's holes come through as rings of their own, which is
   * what makes a courtyard show as a courtyard rather than as part of the
   * outline around it. */
  const outlines = (shape) => {
    const kind = shape.type;
    if (kind === "GeometryCollection") return shape.geometries.flatMap(outlines);
    if (kind === "LineString") return [{ ring: shape.coordinates, closed: false }];
    if (kind === "MultiLineString") {
      return shape.coordinates.map((line) => ({ ring: line, closed: false }));
    }
    if (kind === "Polygon") return shape.coordinates.map((ring) => ({ ring, closed: true }));
    if (kind === "MultiPolygon") {
      return shape.coordinates.flatMap((polygon) =>
        polygon.map((ring) => ({ ring, closed: true }))
      );
    }
    // A point or a multipoint has no outline; its vertices are drawn as marks.
    return [];
  };

  // -------------------------------------------------------------------------
  // The map
  // -------------------------------------------------------------------------

  const SVG_NS = "http://www.w3.org/2000/svg";

  const svg = (tag, attributes = {}) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (value !== null && value !== undefined) node.setAttribute(name, String(value));
    }
    return node;
  };

  /* Which kinds a person can actually change by pointing at the map.
   *
   * A multi-geometry or a collection is drawn faithfully but edited as text.
   * The gestures for "add a vertex to the third ring of the second polygon"
   * are a whole interface of their own, and one built badly is worse than the
   * textarea beside it, which already holds exactly what the server parses. */
  const EDITABLE = new Set(["POINT", "LINESTRING", "POLYGON"]);

  class GeometryMap {
    constructor(input) {
      this.input = input;
      this.template = input.dataset.ffMap;
      this.kind = (input.dataset.ffGeometryKind || "POINT").toUpperCase();
      this.srid = Number(input.dataset.ffSrid) || 4326;
      this.editable = EDITABLE.has(this.kind);
      this.shape = this.read();

      const points = everyPoint(this.shape);
      const fallback = parsePoint(input.dataset.ffMapCenter) || { lat: 0, lng: 0 };
      this.center = points.length ? centreOf(points) : fallback;
      // Zoomed out until there is something to look at: a street-level view of
      // nowhere in particular is less use than seeing which continent it is.
      this.zoom = points.length ? (points.length > 1 ? 11 : 13) : 2;
      this.build();
    }

    /* The control's text as a shape, or null.
     *
     * A POINT box holds "lat, lng" because that is what a person types and what
     * every map, phone and street sign uses. Everything else holds EWKT,
     * because a polygon has no short human form and the server has to be able
     * to read back whatever was rendered. */
    read() {
      const raw = this.input.value;
      if (this.kind === "POINT") {
        const point = parsePoint(raw);
        return point && { type: "Point", coordinates: [point.lng, point.lat] };
      }
      return readWkt(raw);
    }

    /* The vertices this map lets a pointer move.
     *
     * A polygon's outer ring only. Its holes are kept exactly as they arrived
     * and written back untouched -- dragging a vertex must not quietly discard
     * the courtyard out of a building's footprint just because this control has
     * no gesture for one. */
    vertices() {
      if (!this.shape || !this.editable) return [];
      if (this.kind === "POINT") return [this.shape.coordinates];
      if (this.kind === "LINESTRING") return this.shape.coordinates;
      return this.shape.coordinates[0] || [];
    }

    setVertices(points) {
      if (!points.length) {
        this.shape = null;
        return;
      }
      if (this.kind === "POINT") {
        this.shape = { type: "Point", coordinates: points[points.length - 1] };
        return;
      }
      if (this.kind === "LINESTRING") {
        this.shape = { type: "LineString", coordinates: points };
        return;
      }
      const holes = this.shape && this.shape.type === "Polygon"
        ? this.shape.coordinates.slice(1)
        : [];
      this.shape = { type: "Polygon", coordinates: [points, ...holes] };
    }

    build() {
      this.tiles = el("div", { class: "ff-map__tiles" });
      /* One <svg> over the tiles for the shape itself, and plain elements above
       * it for the handles. The shape is vector because a polygon is; the
       * handles are not, because they need a cursor, a hover state and a hit
       * area larger than the dot they draw, all of which are free on a <div>
       * and fiddly on an SVG child. */
      this.overlay = svg("svg", { class: "ff-map__overlay", "aria-hidden": "true" });
      this.handles = el("div", { class: "ff-map__handles" });
      // A pin, anchored at its point rather than at its middle -- the place is
      // where the tip touches the map, which is what makes a marker readable at
      // a glance instead of a dot you have to guess the centre of.
      this.marker = el("div", { class: "ff-map__marker", "aria-hidden": "true" }, [
        icon("map-pin", 30),
      ]);
      this.canvas = el("div", { class: "ff-map__canvas" }, [
        this.tiles,
        this.overlay,
        this.handles,
        this.marker,
      ]);
      /* One layer per zoom level, keyed by that zoom. Tiles inside a layer are
       * placed at their own level's pixel coordinates and the layer carries the
       * scale, which is what lets a zoom be applied to what is already on
       * screen in the same frame the button was pressed. */
      this.layers = new Map();
      this.current = null;
      this.backdrop = null;

      const button = (name, label, run) => {
        const control = el(
          "button",
          { type: "button", class: "ff-map__button", "aria-label": label },
          [icon(name, 15)]
        );
        control.addEventListener("click", (event) => {
          // Inside a form: without this the first button in the map submits it.
          event.preventDefault();
          run();
        });
        return control;
      };

      /* Kept, so `draw` can disable them at the ends. A + that silently does
       * nothing is the single thing that makes a map feel broken -- there is no
       * way to tell "at the limit" from "not responding" without being told. */
      this.zoomIn = button("plus", t("ZoomIn"), () => this.zoomBy(1));
      this.zoomOut = button("minus", t("ZoomOut"), () => this.zoomBy(-1));
      const controls = [this.zoomIn, this.zoomOut];

      // Only where the browser offers it: geolocation needs a secure context,
      // and a button that can only ever fail is worse than no button.
      if (navigator.geolocation) {
        controls.push(button("crosshair", t("MyLocation"), () => this.locate()));
      }

      // Undoing a shape vertex by vertex is a gesture; starting again is a
      // button. Only where there is something to clear and a way to redraw it.
      if (this.editable && this.kind !== "POINT") {
        controls.push(
          button("close", t("Clear"), () => {
            this.setVertices([]);
            this.write();
            this.draw();
          })
        );
      }

      const children = [this.canvas, el("div", { class: "ff-map__controls" }, controls)];

      // The credit line goes on the map, where every other map puts it and
      // where it stays attached to the tiles it is crediting. Under the field
      // it read as help text for the input.
      const credit = this.input.dataset.ffMapCredit;
      if (credit) children.push(el("div", { class: "ff-map__credit", text: credit }));

      this.element = el("div", { class: "ff-map ff-js-only" }, children);
      if (!this.editable) this.element.dataset.ffReadonly = "true";

      this.input.after(this.element);
      this.bind();
      // The container has no width until it is in the document.
      this.draw();
      // A form field can be inside a panel that is hidden at build time, where
      // every tile would be laid out against a width of zero.
      new ResizeObserver(() => this.draw()).observe(this.canvas);

      this.input.addEventListener("change", () => {
        /* `write()` puts the edited shape into this input and dispatches this
         * very event, so anyone bound to the field learns the value changed.
         * Without this guard that dispatch loops back here: every click both
         * dropped a vertex and yanked the whole map to centre on it, which
         * reads as the map lurching on every click rather than a point
         * appearing where the pointer was. With a shape being dragged it is
         * worse -- the handler re-parses the box it just wrote and rebuilds
         * the shape from its own rounded text in the middle of the gesture. */
        if (this.settingInput) return;
        const typed = this.read();
        // Unreadable text is someone half-way through typing, not a request to
        // blank the map. The box is the authority on save; until then the last
        // shape that parsed stays on screen.
        if (!typed) return;
        this.shape = typed;
        const points = everyPoint(typed);
        if (points.length) this.center = centreOf(points);
        this.draw();
      });
    }

    /* Centre on where the browser says the device is, and mark it.
     *
     * The commonest thing anyone puts in a location field is where they are
     * standing, and without this that means finding it by dragging from a view
     * of the whole world. Only offered where `navigator.geolocation` exists;
     * a refusal or a timeout leaves the map exactly as it was, because the
     * alternative -- an error banner on a field nobody was required to use --
     * is noise about something that was optional to begin with.
     *
     * On a line or a ring this appends, the same as any other way of naming a
     * place: "here" is one more vertex, not a shape that replaces the one being
     * drawn.
     */
    locate() {
      this.element.dataset.ffLocating = "true";
      const done = () => delete this.element.dataset.ffLocating;

      navigator.geolocation.getCurrentPosition(
        (position) => {
          done();
          const found = {
            lat: position.coords.latitude,
            lng: wrapLng(position.coords.longitude),
          };
          if (this.editable) this.setVertices([...this.vertices(), [found.lng, found.lat]]);
          this.center = found;
          // Close enough to see a street, which is the scale somebody asking
          // "where am I" is asking at.
          this.zoom = Math.max(this.zoom, 15);
          this.write();
          this.draw();
        },
        done,
        { enableHighAccuracy: true, timeout: 10_000 }
      );
    }

    bind() {
      let dragging = null;

      // Belt to the stylesheet's braces: anything the map draws that turns out
      // to be draggable takes the gesture away from the pan.
      this.canvas.addEventListener("dragstart", (event) => event.preventDefault());

      this.canvas.addEventListener("pointerdown", (event) => {
        dragging = { x: event.clientX, y: event.clientY, moved: false };
        this.canvas.setPointerCapture(event.pointerId);
        // The hand closes while the map is being dragged. The cursor is the
        // only thing telling anyone the map can be dragged at all.
        this.canvas.dataset.ffDragging = "true";
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
        delete this.canvas.dataset.ffDragging;
        this.canvas.releasePointerCapture(event.pointerId);
        if (!wasDrag) this.pickAt(event);
      });

      this.canvas.addEventListener("pointercancel", () => {
        dragging = null;
        delete this.canvas.dataset.ffDragging;
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

    /* Drag the map.
     *
     * East and west are unbounded, because the world is: `draw()` already wraps
     * the tile column, so panning past the anti-meridian arrives back where it
     * started. They used to be clamped to the width of the world minus the
     * canvas, and at the zoom levels the field opens on that bound is *smaller*
     * than the canvas -- the low end came out above the high end, `clamp` then
     * returned the high one whatever it was given, and every drag snapped the
     * centre to the same spot. The map looked stuck because it was.
     *
     * North and south stay bounded, because the world genuinely ends there;
     * past the poles there is nothing to show but empty canvas. Zoomed out far
     * enough that the whole world is shorter than the canvas, there is only one
     * position worth being in, so it centres instead of clamping to a range
     * that has crossed over. */
    panBy(dx, dy) {
      const { height } = this.size();
      const span = TILE * 2 ** this.zoom;
      const x = lngToX(this.center.lng, this.zoom) + dx;
      const y = latToY(this.center.lat, this.zoom) + dy;
      const vertical = span <= height ? span / 2 : clamp(y, height / 2, span - height / 2);
      this.center = {
        lat: yToLat(vertical, this.zoom),
        lng: wrapLng(xToLng(x, this.zoom)),
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
        // Wrapped, because the pointer can be over a copy of the world one
        // pan to the east of the first one.
        lng: wrapLng(xToLng(originX + (event.clientX - box.left), this.zoom)),
      };
    }

    /* Put the edited shape into the control that actually submits.
     *
     * A POINT writes "lat, lng", which is what the box showed and what a person
     * would have typed. Everything else writes EWKT with the column's own SRID,
     * because `geo.parse` needs a form it can read back and a polygon has no
     * short one.
     *
     * Synchronous: every listener the dispatch reaches, including the one this
     * class registers on the same control, runs before this returns -- so the
     * guard has to be up around it.
     */
    write() {
      if (!this.shape) {
        this.input.value = "";
      } else if (this.kind === "POINT") {
        const [lng, lat] = this.shape.coordinates;
        this.input.value = formatPoint({ lat, lng });
      } else {
        this.input.value = `SRID=${this.srid};${wktBody(this.closed())}`;
      }
      this.settingInput = true;
      this.input.dispatchEvent(new Event("change", { bubbles: true }));
      this.settingInput = false;
    }

    /* A polygon ring with its first vertex repeated at the end.
     *
     * WKT requires the ring to close and PostGIS rejects one that does not, but
     * carrying the duplicate around while editing means every drag of the first
     * handle has to remember to move a second, invisible one -- and the first
     * time it was forgotten the ring tore open on save. Held open, closed once,
     * here.
     */
    closed() {
      if (this.kind !== "POLYGON" || !this.shape) return this.shape;
      const rings = this.shape.coordinates.map((ring) => {
        if (ring.length < 3) return ring;
        const [first] = ring;
        const last = ring[ring.length - 1];
        return first[0] === last[0] && first[1] === last[1] ? ring : [...ring, first];
      });
      return { type: "Polygon", coordinates: rings };
    }

    /* A click on the map itself.
     *
     * For a point it moves the one vertex there is. For a line or a ring it
     * appends, because that is what drawing is: the shape grows in the order
     * the places were visited. Moving and removing are the handles' job.
     */
    pickAt(event) {
      if (!this.editable) return;
      const { lat, lng } = this.atEvent(event);
      this.setVertices([...this.vertices(), [lng, lat]]);
      this.write();
      this.draw();
    }

    /* Drag a vertex, or remove it.
     *
     * Removal is the shift key rather than a double click: a double click on a
     * map already means "zoom in" everywhere else, and a handle sitting under
     * the pointer is exactly where someone would double-click to zoom into the
     * detail they are trying to see.
     */
    grabHandle(event, index) {
      if (!this.editable) return;
      event.preventDefault();
      event.stopPropagation();

      if (event.shiftKey) {
        const points = this.vertices().filter((_, at) => at !== index);
        this.setVertices(points);
        this.write();
        this.draw();
        return;
      }

      const handle = event.currentTarget;
      handle.setPointerCapture(event.pointerId);

      const move = (moved) => {
        const { lat, lng } = this.atEvent(moved);
        const points = this.vertices().map((point, at) => (at === index ? [lng, lat] : point));
        this.setVertices(points);
        this.draw();
      };
      const drop = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", drop);
        handle.removeEventListener("pointercancel", drop);
        // Written once, at the end. Writing on every pointermove dispatches a
        // change event per frame, and anything listening for one -- the unsaved
        // -changes guard above all -- fires a few hundred times per drag.
        this.write();
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", drop);
      handle.addEventListener("pointercancel", drop);
    }

    /* Where a [lng, lat] pair sits on the canvas right now.
     *
     * Panning is unbounded, so a coordinate may belong to a copy of the world
     * several widths away from the one on screen. Drawn at its raw offset it
     * slides off the edge and never comes back; drawn in the nearest copy it
     * stays where the place actually is.
     */
    project(point, originX, width, span) {
      const centre = width / 2;
      const offset = lngToX(point[0], this.zoom) - originX - centre;
      return {
        x: centre + nearestCopy(offset, TILE * span),
        y: latToY(point[1], this.zoom) - this.originY,
      };
    }

    /* The shape, and one handle per vertex a pointer can move.
     *
     * Rebuilt from scratch on every draw rather than diffed. A pan is already
     * repositioning every tile on the map; a handful of vertices is nothing
     * beside that, and the version that tried to move existing nodes had a
     * class of bug -- a stale handle left behind after a vertex was removed --
     * that this simply cannot have.
     */
    drawShape(originX, width, span) {
      this.overlay.replaceChildren();
      this.handles.replaceChildren();

      // A POINT keeps the pin it always had: a marker anchored at its tip reads
      // as a place, where a circle reads as a vertex of something unfinished.
      const isPoint = this.shape && this.shape.type === "Point";
      this.marker.hidden = !isPoint;
      if (isPoint) {
        const at = this.project(this.shape.coordinates, originX, width, span);
        this.marker.style.transform = `translate(${at.x}px, ${at.y}px)`;
      }

      if (!this.shape) return;

      if (!isPoint) {
        for (const path of outlines(this.shape)) {
          const points = path.ring
            .map((point) => {
              const at = this.project(point, originX, width, span);
              return `${at.x},${at.y}`;
            })
            .join(" ");
          this.overlay.append(
            svg(path.closed ? "polygon" : "polyline", {
              class: path.closed ? "ff-map__area" : "ff-map__line",
              points,
            })
          );
        }
      }

      if (!this.editable || isPoint) return;

      this.vertices().forEach((point, index) => {
        const at = this.project(point, originX, width, span);
        const handle = el("button", {
          type: "button",
          class: "ff-map__handle",
          // Numbered from one: this is a label a person reads, not an index.
          "aria-label": `${index + 1}`,
        });
        handle.style.transform = `translate(${at.x}px, ${at.y}px)`;
        handle.addEventListener("pointerdown", (event) => this.grabHandle(event, index));
        // A handle inside a form is a button, and a button inside a form
        // submits it. Every gesture here is a pointer gesture, so the click
        // that follows one has nothing left to do.
        handle.addEventListener("click", (event) => event.preventDefault());
        this.handles.append(handle);
      });
    }
    /* The layer holding one zoom level's tiles, created on first use.
     *
     * The level that was on screen when a new one appears is kept underneath as
     * a backdrop: scaled to line up with the new zoom, it fills the view for the
     * fraction of a second before the sharp tiles arrive. Removing it instead --
     * or leaving it at the old zoom's scale, which amounts to the same thing --
     * is what made a zoom blank the map, then repaint. Only one is kept, and an
     * incoming level that never managed to load a tile does not become one,
     * because an empty backdrop is the blank frame it exists to prevent. */
    layerFor(zoom) {
      const found = this.layers.get(zoom);
      if (found) return found;

      const outgoing = this.layers.get(this.current);
      if (outgoing && outgoing.loaded > 0) this.backdrop = this.current;

      for (const [level, layer] of this.layers) {
        if (level !== this.backdrop) {
          layer.element.remove();
          this.layers.delete(level);
        }
      }

      const layer = {
        zoom,
        element: el("div", { class: "ff-map__layer" }),
        tiles: new Map(),
        pending: 0,
        loaded: 0,
      };
      this.tiles.append(layer.element);
      this.layers.set(zoom, layer);
      return layer;
    }

    /* Place every layer for the view as it is now. A layer drawn at zoom `z`
     * holds world pixels of that level, so scaling it by 2^(zoom - z) puts them
     * where this zoom's pixels are and the backdrop stays registered with the
     * level on top of it rather than sliding around under it. */
    position(originX, originY) {
      for (const layer of this.layers.values()) {
        const scale = 2 ** (this.zoom - layer.zoom);
        layer.element.style.transform =
          `translate(${-originX}px, ${-originY}px) scale(${scale})`;
      }
    }

    draw() {
      const { width, height } = this.size();
      if (!width || !height) return;

      const originX = lngToX(this.center.lng, this.zoom) - width / 2;
      const originY = latToY(this.center.lat, this.zoom) - height / 2;

      /* Tiles come from the deepest level the server actually has; the view can
       * go further than that.
       *
       * Pressing + at the old ceiling did nothing at all -- no movement, no
       * disabled button, nothing -- which reads as the map having broken rather
       * than as the map having run out of pictures. Every map application in
       * the world keeps zooming past its imagery and simply shows it larger, so
       * this does the same: past `MAX_TILE_ZOOM` the last level is scaled up,
       * which the layer transform was already doing for the fraction of a
       * second after every other zoom.
       *
       * `factor` converts between the two. Tile indices are worked out in the
       * tile level's own pixels, because that is the grid the server serves;
       * the marker, the shape and the handles stay in the view's pixels, which
       * is why only this block is converted and nothing below it is. */
      const tileZoom = Math.min(this.zoom, MAX_TILE_ZOOM);
      const factor = 2 ** (this.zoom - tileZoom);
      const span = 2 ** tileZoom;
      const tileOriginX = originX / factor;
      const tileOriginY = originY / factor;
      const first = { x: Math.floor(tileOriginX / TILE), y: Math.floor(tileOriginY / TILE) };
      const last = {
        x: Math.floor((tileOriginX + width / factor) / TILE),
        y: Math.floor((tileOriginY + height / factor) / TILE),
      };

      this.zoomIn.disabled = this.zoom >= MAX_ZOOM;
      this.zoomOut.disabled = this.zoom <= MIN_ZOOM;

      const layer = this.layerFor(tileZoom);
      this.current = tileZoom;
      // Zooming out and back lands on a level that already exists but sits
      // under the one that replaced it, where a stale half-screen of tiles
      // would cover the fresh ones. Appending a node already in the tree moves
      // it; it does not re-request the images.
      if (this.tiles.lastElementChild !== layer.element) this.tiles.append(layer.element);

      const wanted = new Set();
      for (let x = first.x; x <= last.x; x += 1) {
        for (let y = first.y; y <= last.y; y += 1) {
          // Rows past the edge do not exist; columns wrap, because the world does.
          if (y < 0 || y >= span) continue;
          wanted.add(`${x},${y}`);
          if (layer.tiles.has(`${x},${y}`)) continue;

          const column = ((x % span) + span) % span;
          // Not lazy: these are the tiles of the view being drawn right now, and
          // a deferred one never fires the event that retires the backdrop.
          //
          // `draggable="false"` alongside the stylesheet's `pointer-events`,
          // because the attribute is what older browsers honour: an image is
          // draggable by default, and pressing on one to pan the map started
          // the browser's own drag-and-drop instead.
          const image = el("img", {
            class: "ff-map__tile",
            alt: "",
            decoding: "async",
            draggable: "false",
          });
          // Positioned in the layer's own coordinates. The layer's transform is
          // what turns these into screen pixels, which is why a zoom moves one
          // element instead of every tile.
          image.style.transform = `translate(${x * TILE}px, ${y * TILE}px)`;
          layer.pending += 1;

          // Counted exactly once, however it ends: loaded, failed, or panned off
          // the edge before it arrived. A tile that is never accounted for holds
          // `pending` above zero for good, and the backdrop underneath the level
          // never goes away.
          const source = this.template
            .replace("{z}", String(tileZoom))
            .replace("{x}", String(column))
            .replace("{y}", String(y));

          let counted = false;
          const done = (shown) => {
            if (counted) return;
            counted = true;
            layer.pending -= 1;
            if (shown) {
              image.dataset.ffLoaded = "true";
              layer.loaded += 1;
            }
            // The level is complete: nothing is showing through it any more.
            if (layer.pending === 0 && this.layers.get(this.current) === layer) this.settle(layer);
          };

          /* One retry, then give up.
           *
           * A tile that fails stays at `opacity: 0` -- a hole in the map, in the
           * shape of a rectangle, which reads as the map being broken. The
           * commonest cause is a tile server throttling a burst, and throttling
           * is temporary: asking again a moment later usually works, and asking
           * again immediately would be another request in the same burst that
           * caused it. Once only, because a server that has refused twice is
           * saying something, and a map that keeps retrying is the behaviour
           * their usage policies exist to stop.
           */
          let retried = false;
          image.addEventListener("load", () => done(true), { once: true });
          image.addEventListener("error", () => {
            if (retried || counted) {
              done(false);
              return;
            }
            retried = true;
            setTimeout(() => {
              if (!counted) image.src = `${source}${source.includes("?") ? "&" : "?"}retry=1`;
            }, 900);
          });
          // Called when the tile is discarded before it settled. Removing an
          // <img> mid-request does not reliably fire either event.
          image.ffRetire = () => done(false);
          image.src = source;

          layer.element.append(image);
          layer.tiles.set(`${x},${y}`, image);
        }
      }

      // Tiles this level no longer needs. Safe to drop immediately, unlike a
      // whole level: they are off the edge of a pan, not under what replaced them.
      for (const [key, image] of layer.tiles) {
        if (!wanted.has(key)) {
          image.ffRetire();
          image.remove();
          layer.tiles.delete(key);
        }
      }

      this.position(originX, originY);
      if (layer.pending === 0) this.settle(layer);

      // Kept for `project`, which is called once per vertex and would otherwise
      // recompute the same origin for each of them.
      this.originY = originY;
      // `2 ** this.zoom`, not the tile level's span: the shape and the handles
      // are placed in the *view's* pixels, and `nearestCopy` needs the width of
      // the world at that zoom to find which copy of it the map is over. Past
      // MAX_TILE_ZOOM the two differ, and using the tile span here put every
      // vertex a whole world to one side.
      this.drawShape(originX, width, 2 ** this.zoom);
    }

    /* Every tile of the level on screen has loaded or failed, so whatever was
     * showing through it can go. */
    settle(layer) {
      if (this.backdrop === null && this.layers.size === 1) return;
      for (const [level, other] of this.layers) {
        if (other !== layer) {
          other.element.remove();
          this.layers.delete(level);
        }
      }
      this.backdrop = null;
    }
  }

  /* Built when it is scrolled near, not when the page loads.
   *
   * A model with one geometry column was fine. A model with nine -- which is
   * exactly what a page full of shapes is -- fired every map's first draw at
   * once, and each of those asks for twenty-odd tiles: about two hundred
   * requests in one burst, from one browser, to one tile server. OpenStreetMap's
   * usage policy says not to, and it answers by throttling; a throttled tile
   * fails to load, and a tile that fails to load is `opacity: 0`. The result was
   * a map with a rectangular hole in it, which reads as the map being broken
   * rather than as the tiles having been refused.
   *
   * `rootMargin` builds the map a screen early, so scrolling to a field finds it
   * already drawn rather than watching it fill in. Without IntersectionObserver
   * -- and in a fragment swapped in by the live list, where the element may
   * already be on screen -- it falls back to building immediately, which is the
   * behaviour this replaces and is still correct, only chattier.
   */
  const build = (control) => new GeometryMap(control);

  const watcher =
    typeof IntersectionObserver === "function"
      ? new IntersectionObserver(
          (entries, self) => {
            for (const entry of entries) {
              if (!entry.isIntersecting) continue;
              self.unobserve(entry.target);
              build(entry.target);
            }
          },
          { rootMargin: "100% 0px" }
        )
      : null;

  /* Both controls, because a POINT is one line and everything else is a
   * textarea -- see `geometry_control` in `model/_widgets.html`. */
  register((scope) => {
    for (const control of scope.querySelectorAll("[data-ff-map]")) {
      if (!once(control, "ffMapReady")) continue;
      if (watcher) watcher.observe(control);
      else build(control);
    }
  });
})();
