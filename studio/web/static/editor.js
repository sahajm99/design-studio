// Design Studio: the editor, where the designer places the words, the logo, shades, their
// own lines of text, uploaded images and the photo by hand. No build step, no framework:
// pointer events only.
//
// The canvas is the post at half size (540 by 675 on a wide screen, scaled down whole on
// a phone), drawn the way the renderer draws a custom layout: the canvas colour, the photo
// in its box with its fit and position, then the blocks in the order of their list, a
// later block over an earlier one. Every place is kept in percent of the canvas, in an
// object that mirrors CustomLayout, and Preview posts exactly that object. The photo is
// chosen, moved and resized like a block, as the bottom layer. Dragging, resizing and
// nudging keep the words and the logo inside the 4% margin, every shade, image and the
// photo on the canvas, and the logo at least 14% wide: the rules the renderer applies
// before it draws (see studio/render/custom.py), so what the designer sees is what the
// renderer draws. Images come from the brand's uploads, through the inline picker. The Ask
// panel (v5) sends what the designer types, with a file when the change needs one, to the
// editor agent, and loads the layout it answers with, keeping the one before for Undo.
(() => {
  "use strict";

  // ------------------------------------------------------------ state

  // The renderer's guardrails, in percent of the canvas.
  const MARGIN = 4;
  const FAR = 100 - MARGIN;
  const MIN_LOGO = 14;
  // Sums of percentages carry rounding noise; an edge this close to the margin is on it.
  const EPSILON = 1e-6;
  // A text block's size when size_px is 0, and the range a size that is set keeps to. The
  // designer's own text block starts at the headline's size, as the renderer sets it.
  const DEFAULT_PX = { headline: 64, subline: 26, text: 64 };
  const MIN_PX = 12;
  const MAX_PX = 400;
  // The range a weight that is set keeps to; 0 is the kit's weight for the role.
  const MIN_WEIGHT = 100;
  const MAX_WEIGHT = 900;
  // The most blocks a layout keeps (the renderer's MAX_BLOCKS), the most words a text block
  // holds, and the least an image may be each way, in percent of the canvas (the
  // guardrails' MIN_IMAGE_PCT).
  const MAX_BLOCKS = 12;
  const MAX_TEXT_CHARS = 200;
  const MIN_IMAGE = 4;
  // The smallest each kind can be made by hand, in percent: [width, height]. The words
  // and the logo take their height from their width, so only their width has a minimum.
  const SMALLEST = {
    headline: [4, 0],
    subline: [4, 0],
    text: [4, 0],
    logo: [MIN_LOGO, 0],
    shade: [4, 4],
    image: [MIN_IMAGE, MIN_IMAGE],
    photo: [10, 10],
  };
  // The handles each kind shows, by compass point. The words change width only, so they
  // have no top or bottom handle; the logo keeps its proportions, so it has corners only.
  const EVERY_HANDLE = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
  const WIDTH_HANDLES = ["nw", "ne", "e", "se", "sw", "w"];
  const HANDLES = {
    headline: WIDTH_HANDLES,
    subline: WIDTH_HANDLES,
    text: WIDTH_HANDLES,
    logo: ["nw", "ne", "se", "sw"],
    shade: EVERY_HANDLE,
    image: EVERY_HANDLE,
    photo: EVERY_HANDLE,
  };
  // An arrow key nudges the chosen item this far, in percent of the canvas.
  const NUDGE = 1;
  const SHIFT_NUDGE = 5;
  const ARROWS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  // Each press of a pan arrow moves the photo this far, within PAN_LIMIT either way.
  const PAN_STEP = 2;
  const PAN_LIMIT = 50;
  const PAN_DIRECTIONS = { left: [-1, 0], right: [1, 0], up: [0, -1], down: [0, 1] };
  // The photo's box while it covers the whole canvas, which the layout keeps as null.
  const WHOLE_CANVAS = Object.freeze({ x: 0, y: 0, w: 100, h: 100 });
  // The fields every block carries besides its kind and box, at the values CustomLayout
  // gives a block that leaves them out.
  const BLOCK_DEFAULTS = {
    align: "left",
    size_px: 0,
    colour: "",
    opacity: 0.7,
    font: "",
    weight: 0,
    italic: false,
    text: "",
    image_path: "",
    fit: "contain",
    keep_aspect: true,
  };
  // A new shade: a band across the lower part of the canvas, behind the words.
  const NEW_SHADE = { ...BLOCK_DEFAULTS, kind: "shade", x: 4, y: 56, w: 92, h: 40 };
  // A new line of text: 60% wide in the middle of the canvas, its words centred.
  const NEW_TEXT_WORDS = "New text";
  const NEW_TEXT = {
    ...BLOCK_DEFAULTS,
    kind: "text",
    x: 20,
    y: 46,
    w: 60,
    h: 0,
    align: "centre",
    text: NEW_TEXT_WORDS,
  };
  // A new image: a third of the canvas wide, whole and opaque; its height follows the file.
  const NEW_IMAGE_WIDTH = 100 / 3;
  const NEW_IMAGE = { ...BLOCK_DEFAULTS, kind: "image", opacity: 1, fit: "contain" };
  // The role whose colour a block takes when its colour is the brand's (""). The
  // designer's own text takes the body's, as the renderer gives it.
  const ROLES = { headline: "headline", subline: "body", text: "body", shade: "background" };
  // The blocks a designer can delete, and move up or down the list. The headline, the
  // subline and the logo keep their places; a shade has its own Remove as well.
  const DELETABLE = ["text", "image", "shade"];
  const ORDERABLE = ["text", "image", "shade"];
  const KIND_NAMES = {
    headline: "Headline",
    subline: "Subline",
    text: "Text",
    logo: "Logo",
    shade: "Shade",
    image: "Image",
    photo: "Photo",
  };
  const RENDERING = "Rendering the preview…";
  const PREVIEW_FAILED = "The preview could not be rendered. Try again.";
  const SEE_THE_RUN = "See the run";
  const UPLOADING = "Uploading…";
  const UPLOAD_FAILED = "The image could not be uploaded. Try again.";
  const UPLOADS_FAILED = "Your uploads could not be loaded. Try again.";
  const UNTITLED_UPLOAD = "Untitled";
  // The Ask panel's line while the editor answers, when it cannot be reached, after edits
  // that came without a summary, and when the canvas changed before the answer came.
  const ASK_WORKING = "Working…";
  const ASK_FAILED = "The editor could not answer. Try again.";
  const ASK_CHANGED = "The layout was changed.";
  const ASK_STALE = "The layout changed while the editor answered. Apply again.";
  // The canvas names each face by its place in the list, so no face's own name can clash
  // with a font the page itself uses.
  const FACE_FAMILY = "Editor face";
  // The gap kept under the headline when the subline is moved clear of it, in percent.
  const WORD_GAP = 2;
  // What can be chosen besides a block, which is chosen by its index: the photo, or nothing.
  const PHOTO = "photo";
  const NONE = -1;

  // The page's state: the arrangement and the mode, what is chosen (a block's index,
  // PHOTO or NONE), how many changes the canvas has had (so a preview, or the Ask panel's
  // answer, knows whether it still fits), whether the faces in use have loaded (so the
  // words are measured in them), the brand's uploads as the picker last listed them, the
  // arrangement and the words before the Ask panel's last change, which Undo brings back
  // (null when there is none, or once the designer changes the canvas by hand), whether
  // such a layout is being loaded now, and the Ask panel's last upload ({ file, id }), so a
  // file chosen once goes to the shelf once.
  const editor = {
    data: null,
    layout: null,
    mode: "dark",
    selected: NONE,
    changes: 0,
    fontLoaded: false,
    uploads: [],
    undo: null,
    loading: false,
    askUpload: null,
  };
  // The page's elements, found once.
  const ui = {};

  function setText(element, value) {
    if (element) element.textContent = value;
  }

  function within(value, low, high) {
    return Math.min(Math.max(value, low), Math.max(low, high));
  }

  // The blocks that hold words: the headline, the subline and the designer's own text.
  function isText(kind) {
    return kind === "headline" || kind === "subline" || kind === "text";
  }

  // A shade, an image and the photo have a height of their own; the words and the logo
  // take theirs from their width.
  function hasOwnHeight(kind) {
    return kind === "shade" || kind === "image" || kind === "photo";
  }

  // An upload's media path, as the layout keeps it, from its address under /media/, and
  // back: the media route serves the data folder's files at /media/<media path>.
  function mediaPath(url) {
    return url.replace(/^\/media\//, "");
  }

  function mediaUrl(path) {
    return `/media/${path}`;
  }

  // ------------------------------------------------------------ geometry and rules

  // One side of a box moved, then shrunk, until it sits between low and high, the
  // margin's edges unless told otherwise. A span that starts at or past high cannot
  // shrink inside, so it moves back in.
  function spanInside(start, length, low = MARGIN, high = FAR) {
    const near = Math.max(start, low);
    if (near + length <= high + EPSILON) return [near, length];
    if (near < high) return [near, high - near];
    const fitted = Math.min(length, high - low);
    return [high - fitted, fitted];
  }

  // The edges an item keeps to: the margin for the words and the logo, the canvas for a
  // shade, which may run to the edge as a template's panel does, and for the photo.
  function edges(kind) {
    return hasOwnHeight(kind) ? [0, 100] : [MARGIN, FAR];
  }

  // The renderer's rules for one block, applied to the starting arrangement, so the
  // canvas begins where the renderer would draw it and Preview reports no clamp.
  function guard(block) {
    const [low, high] = edges(block.kind);
    [block.x, block.w] = spanInside(block.x, block.w, low, high);
    if (hasOwnHeight(block.kind)) [block.y, block.h] = spanInside(block.y, block.h, low, high);
    else block.y = Math.max(block.y, MARGIN);
    if (block.kind === "logo" && block.w < MIN_LOGO) {
      block.w = MIN_LOGO;
      block.x = Math.min(block.x, FAR - MIN_LOGO);
    }
    if (block.kind === "image" && block.w < MIN_IMAGE) {
      block.w = MIN_IMAGE;
      block.x = Math.min(block.x, 100 - MIN_IMAGE);
    }
    if (!paletteHex(block.colour)) block.colour = "";
    if (block.font && !faceNamed(block.font)) block.font = "";
    // Italics come only from the face's italic file, as the renderer keeps them, never from
    // a slant the browser fakes from the upright file.
    const face = faceOf(block);
    if (block.italic && !(face && face.italic_url)) block.italic = false;
    if (block.size_px) block.size_px = Math.round(within(block.size_px, MIN_PX, MAX_PX));
    if (block.weight) block.weight = Math.round(within(block.weight, MIN_WEIGHT, MAX_WEIGHT));
    block.opacity = within(block.opacity, 0, 1);
  }

  // The photo's box as the renderer keeps it: on the canvas, and at least its smallest
  // size each way, moving back from the far edge to make room.
  function guardPhoto(box) {
    if (!box) return;
    const [least] = SMALLEST.photo;
    [box.x, box.w] = spanInside(box.x, box.w, 0, 100);
    [box.y, box.h] = spanInside(box.y, box.h, 0, 100);
    if (box.w < least) [box.x, box.w] = [Math.min(box.x, 100 - least), least];
    if (box.h < least) [box.y, box.h] = [Math.min(box.y, 100 - least), least];
  }

  // An item's height in percent of the canvas. The words and the logo take their natural
  // height, so theirs is measured; a shade and the photo have their own.
  function heightOf(kind, box, element) {
    if (hasOwnHeight(kind)) return box.h;
    return element ? (element.offsetHeight / ui.canvas.clientHeight) * 100 : 0;
  }

  // The words and the logo grow downwards as they wrap or widen. One that would run past
  // the margin's bottom edge moves up instead. Until the faces in use have loaded, the
  // words would be measured in another font, so nothing moves.
  function keepInside(kind, box, element) {
    if (!editor.fontLoaded || hasOwnHeight(kind)) return;
    const height = heightOf(kind, box, element);
    if (box.y + height > FAR + EPSILON) box.y = Math.max(MARGIN, FAR - height);
  }

  // An item moved by (dx, dy) from where it was (`from`), its size unchanged, within its
  // edges. `height` is its height in percent.
  function moveItem(kind, box, from, dx, dy, height) {
    const [low, high] = edges(kind);
    box.x = within(from.x + dx, low, high - box.w);
    box.y = within(from.y + dy, low, high - height);
  }

  // The sides a handle moves, across and down: -1 for the left or the top, 1 for the
  // right or the bottom, 0 for neither.
  function handleSides(handle) {
    const across = handle.includes("w") ? -1 : handle.includes("e") ? 1 : 0;
    const down = handle.includes("n") ? -1 : handle.includes("s") ? 1 : 0;
    return [across, down];
  }

  // One side of a box resized from a handle: the moving edge follows the pointer, the
  // opposite edge stays, and the length keeps between `least` and the room to the edge.
  function resizeSpan(start, length, side, delta, least, low, high) {
    if (side > 0) return [start, within(length + delta, least, high - start)];
    if (side < 0) {
      const end = start + length;
      const near = within(start + delta, low, end - least);
      return [near, end - near];
    }
    return [start, length];
  }

  // An item resized from a handle by (dx, dy), from its box at the start (`from`). A shade,
  // the photo and an image that does not keep its aspect resize freely; the words change
  // width only, from a side or a corner; the logo and an image that keeps its aspect keep
  // their proportions.
  function resizeItem(kind, box, from, handle, dx, dy) {
    const [across, down] = handleSides(handle);
    if (kind === "logo") {
      resizeLogo(box, from, across, down, dx, dy);
      return;
    }
    if (kind === "image" && box.keep_aspect) {
      resizeInProportion(box, from, across, down, dx, dy);
      return;
    }
    const [low, high] = edges(kind);
    const [leastWidth, leastHeight] = SMALLEST[kind];
    [box.x, box.w] = resizeSpan(from.x, from.w, across, dx, leastWidth, low, high);
    if (hasOwnHeight(kind)) {
      [box.y, box.h] = resizeSpan(from.y, from.h, down, dy, leastHeight, low, high);
    }
  }

  // The logo resized from a corner: the opposite corner stays, the width follows the way
  // the pointer moved further, and the height follows the file. It is never narrower than
  // the renderer allows, nor past the margin. `from.height` is its height at the start.
  function resizeLogo(logo, from, across, down, dx, dy) {
    const tall = from.w > 0 ? from.height / from.w : 0; // height per unit of width
    const wider = across * dx;
    const taller = tall > 0 ? (down * dy) / tall : 0;
    const grow = Math.abs(taller) > Math.abs(wider) ? taller : wider;
    const right = from.x + from.w;
    const bottom = from.y + from.height;
    const roomAcross = across > 0 ? FAR - from.x : right - MARGIN;
    const roomDown = tall > 0 ? (down > 0 ? FAR - from.y : bottom - MARGIN) / tall : Infinity;
    logo.w = within(from.w + grow, MIN_LOGO, Math.min(roomAcross, roomDown));
    logo.x = across > 0 ? from.x : right - logo.w;
    logo.y = down > 0 ? from.y : Math.max(MARGIN, bottom - logo.w * tall);
  }

  // An image that keeps its aspect, resized from any handle: both sides scale by one
  // factor, so the picture keeps its proportions in its box. The edge or corner opposite
  // the handle stays put; a side handle grows the other way from the top or the left edge.
  // A corner follows the way the pointer moved further. The box never grows past the room
  // on the canvas either way, which wins over an image's smallest size when the two clash
  // (a very wide image near an edge), and the result is held on the canvas, so rounding
  // never leaves an edge a hair past it, where the server refuses the box.
  function resizeInProportion(box, from, across, down, dx, dy) {
    if (from.w <= 0 || from.h <= 0) return;
    const wider = across ? (from.w + across * dx) / from.w : null;
    const taller = down ? (from.h + down * dy) / from.h : null;
    let scale = wider === null ? taller : wider;
    if (wider !== null && taller !== null && Math.abs(taller - 1) > Math.abs(wider - 1)) {
      scale = taller;
    }
    const right = from.x + from.w;
    const bottom = from.y + from.h;
    const roomAcross = across < 0 ? right : 100 - from.x;
    const roomDown = down < 0 ? bottom : 100 - from.y;
    const [leastWidth, leastHeight] = SMALLEST.image;
    const low = Math.max(leastWidth / from.w, leastHeight / from.h);
    const high = Math.min(roomAcross / from.w, roomDown / from.h);
    scale = within(scale, Math.min(low, high), high);
    box.w = Math.min(100, from.w * scale);
    box.h = Math.min(100, from.h * scale);
    box.x = Math.max(0, across < 0 ? right - box.w : from.x);
    box.y = Math.max(0, down < 0 ? bottom - box.h : from.y);
  }

  // A starting arrangement sets the subline a fixed distance under the headline, which
  // a longer headline runs into. Once, on open, in the faces in use: a subline in the
  // headline's column, starting at or below the headline's top but above its bottom
  // plus a small gap, moves down to that line. The margin pass that follows keeps it
  // inside the margin.
  function clearSubline() {
    const blocks = editor.layout.blocks;
    const headlineIndex = blocks.findIndex((block) => block.kind === "headline");
    const sublineIndex = blocks.findIndex((block) => block.kind === "subline");
    if (headlineIndex < 0 || sublineIndex < 0) return;
    const headline = blocks[headlineIndex];
    const subline = blocks[sublineIndex];
    const element = blockElement(headlineIndex);
    if (!element) return;
    const sameColumn = subline.x < headline.x + headline.w && headline.x < subline.x + subline.w;
    const clear = headline.y + heightOf(headline.kind, headline, element) + WORD_GAP;
    if (sameColumn && subline.y >= headline.y && subline.y < clear) subline.y = clear;
  }

  // ------------------------------------------------------------ colours and faces

  function paletteHex(name) {
    const colour = editor.data.palette.find((entry) => entry.name === name);
    return colour ? colour.hex : null;
  }

  function roleHex(role) {
    return editor.data.roles[editor.mode][role];
  }

  // The colour a block is drawn in: its palette colour, or its role's in the post mode.
  function blockHex(block) {
    return paletteHex(block.colour) || roleHex(ROLES[block.kind]);
  }

  function logoUrl() {
    return `/brand/logo?mode=${editor.mode}`;
  }

  // The words as the two inputs hold them now. The browser may have kept what was
  // typed before a reload, so the page data's words are only where the inputs began.
  function wordsOf(kind) {
    const input = ui.words[kind];
    return input ? input.value : "";
  }

  function faceNamed(name) {
    return editor.data.faces.find((face) => face.name === name) || null;
  }

  // The place in the list of the face a block's words are set in: the face it names, or
  // the kit's family, which the list gives first.
  function faceIndex(block) {
    return Math.max(0, editor.data.faces.findIndex((face) => face.name === block.font));
  }

  function faceOf(block) {
    return editor.data.faces[faceIndex(block)] || null;
  }

  // The name the canvas knows a face by, as a CSS string.
  function faceFamily(index) {
    return `"${FACE_FAMILY} ${index}"`;
  }

  // A block's weight: its own, or the kit's weight for its role.
  function weightOf(block) {
    const type = editor.data.typography;
    return block.weight || (block.kind === "headline" ? type.headline_weight : type.body_weight);
  }

  // A block's words as a CSS font, for the font loading API.
  function fontOf(block) {
    const style = block.italic ? "italic" : "normal";
    return `${style} ${weightOf(block)} 1em ${faceFamily(faceIndex(block))}`;
  }

  // One @font-face per face and style, in a style element of the page's own, so the
  // canvas sets the words in the real faces. Each covers the whole weight range, as the
  // renderer declares it. The addresses come from the server, already encoded.
  function declareFaces() {
    const rules = editor.data.faces.flatMap((face, index) =>
      [
        [face.regular_url, "normal"],
        [face.italic_url, "italic"],
      ]
        .filter(([url]) => url)
        .map(
          ([url, style]) =>
            `@font-face { font-family: ${faceFamily(index)}; src: url("${url}"); ` +
            `font-weight: 100 900; font-style: ${style}; font-display: block; }`,
        ),
    );
    const sheet = document.createElement("style");
    sheet.textContent = rules.join("\n");
    document.head.append(sheet);
  }

  // Resolves once the faces the words use have loaded, or failed to, so the words are
  // measured in the faces they are drawn in.
  async function facesLoaded() {
    const fonts = editor.layout.blocks.filter((block) => isText(block.kind)).map(fontOf);
    await Promise.allSettled(fonts.map((font) => document.fonts.load(font)));
    await document.fonts.ready;
  }

  // Resolves once the face one block's words use, in its style, has loaded, or failed to.
  // A face or an italic file chosen after the page opened has not loaded yet, and words
  // measured in the fallback font could be pushed up for good.
  async function faceLoaded(block) {
    try {
      await document.fonts.load(fontOf(block));
    } catch {
      // The face failed to load: the words are drawn in the fallback font all the same.
    }
  }

  // ------------------------------------------------------------ the canvas

  function blockElement(index) {
    return ui.canvas.querySelector(`[data-block="${index}"]`);
  }

  function selectedBlock() {
    return editor.layout.blocks[editor.selected] || null;
  }

  function firstLogo() {
    return editor.layout.blocks.find((block) => block.kind === "logo") || null;
  }

  // The photo's box: the layout's own, or the whole canvas while it has none.
  function photoBox() {
    return editor.layout.photo || WHOLE_CANVAS;
  }

  // What is chosen, as the geometry sees it: its kind, the object that holds its box
  // (x, y, w, h) and its element on the canvas; null when nothing is.
  function selectedItem() {
    if (editor.selected === PHOTO) return { kind: "photo", box: photoBox(), element: ui.photoBox };
    const block = selectedBlock();
    if (!block) return null;
    return { kind: block.kind, box: block, element: blockElement(editor.selected) };
  }

  // The box a change works on, and what it was before. The photo gets a box of its own
  // for the change; `settle` says whether the place or the size changed, and leaves a
  // photo that still covers the whole canvas without a box again.
  function workingBox(item) {
    const fresh = item.kind === "photo" && !editor.layout.photo;
    if (fresh) editor.layout.photo = { ...WHOLE_CANVAS };
    const box = item.kind === "photo" ? editor.layout.photo : item.box;
    const from = { ...box };
    const settle = () => {
      const changed = ["x", "y", "w", "h"].some((side) => box[side] !== from[side]);
      if (fresh && !changed) editor.layout.photo = null;
      return changed;
    };
    return { box, from, settle };
  }

  function placeBox(element, box, kind) {
    const style = element.style;
    style.left = `${box.x}%`;
    style.top = `${box.y}%`;
    style.width = `${box.w}%`;
    if (hasOwnHeight(kind)) style.height = `${box.h}%`;
  }

  // Everything about a block but its place: its words, face, size, colour, file and layer.
  // The layer is its place in the list, above the photo's, so a later block sits over an
  // earlier one, as the renderer draws them.
  function paintBlock(element, block) {
    const style = element.style;
    style.zIndex = String(Number(element.dataset.block) + 1);
    if (block.kind === "shade") {
      style.background = blockHex(block);
      style.opacity = String(block.opacity);
    } else if (block.kind === "logo") {
      // An uploaded logo stands in for the kit's file, in the same box.
      const src = block.image_path ? mediaUrl(block.image_path) : logoUrl();
      if (element.getAttribute("src") !== src) element.src = src;
    } else if (block.kind === "image") {
      const src = mediaUrl(block.image_path);
      if (element.getAttribute("src") !== src) element.src = src;
      style.objectFit = block.fit;
      style.opacity = String(block.opacity);
    } else {
      const words = block.kind === "text" ? block.text : wordsOf(block.kind);
      element.textContent = words;
      // The renderer leaves out words that are empty; their place stays visible here.
      element.classList.toggle("is-empty", !words.trim());
      style.setProperty("--px", String(block.size_px || DEFAULT_PX[block.kind]));
      style.fontFamily = `${faceFamily(faceIndex(block))}, sans-serif`;
      style.fontWeight = String(weightOf(block));
      style.fontStyle = block.italic ? "italic" : "normal";
      style.color = blockHex(block);
      style.textAlign = block.align === "centre" ? "center" : "left";
    }
  }

  function buildBlock(block, index) {
    const picture = block.kind === "logo" || block.kind === "image";
    const element = document.createElement(picture ? "img" : "div");
    element.className = `editor-block editor-${block.kind}`;
    element.dataset.block = String(index);
    if (picture) {
      element.alt = "";
      element.draggable = false;
    }
    if (block.kind === "logo") {
      // The logo's height is known once its file has loaded.
      element.addEventListener("load", redraw);
    }
    paintBlock(element, block);
    placeBox(element, block, block.kind);
    return element;
  }

  // The photo in its box with its fit and position, and the canvas colour around it.
  function drawPhoto() {
    const layout = editor.layout;
    placeBox(ui.photoBox, photoBox(), "photo");
    ui.photo.style.objectFit = layout.photo_fit;
    ui.photo.style.objectPosition = `${50 + layout.photo_offset_x}% ${50 + layout.photo_offset_y}%`;
    ui.canvas.style.backgroundColor = paletteHex(layout.background) || roleHex("background");
  }

  // Build every block again, after one was added, removed or moved in the list.
  function drawCanvas() {
    ui.canvas.querySelectorAll("[data-block]").forEach((element) => element.remove());
    editor.layout.blocks.forEach((block, index) => {
      ui.canvas.insertBefore(buildBlock(block, index), ui.selection);
    });
    syncAddButtons();
    redraw();
  }

  // Draw every block again after its look changed (the words, a face, a size, a colour or
  // the mode), keeping the words and the logo inside the margin as they reflow.
  function redraw() {
    editor.layout.blocks.forEach((block, index) => {
      const element = blockElement(index);
      if (!element) return;
      paintBlock(element, block);
      placeBox(element, block, block.kind);
      keepInside(block.kind, block, element);
      placeBox(element, block, block.kind);
    });
    drawPhoto();
    drawSelection();
  }

  function select(target) {
    editor.selected = target;
    drawSelection();
    syncPanel();
  }

  // ------------------------------------------------------------ handles

  // The chosen item's outline, dashed for the photo, with the handles its kind uses. A
  // handle on an edge that touches the canvas's edge is drawn inside the item, where the
  // canvas does not clip it.
  function drawSelection() {
    const item = selectedItem();
    ui.selection.hidden = !(item && item.element);
    if (ui.selection.hidden) return;
    const { kind, box, element } = item;
    const height = heightOf(kind, box, element);
    const style = ui.selection.style;
    style.left = `${box.x}%`;
    style.top = `${box.y}%`;
    style.width = `${box.w}%`;
    style.height = `${height}%`;
    const classes = ui.selection.classList;
    classes.toggle("is-photo", kind === "photo");
    classes.toggle("is-width-only", isText(kind));
    ui.handles.forEach((handle) => {
      handle.hidden = !HANDLES[kind].includes(handle.dataset.editorHandle);
    });
    const reach = handleReach();
    classes.toggle("at-left", box.x < reach.across);
    classes.toggle("at-right", 100 - (box.x + box.w) < reach.across);
    classes.toggle("at-top", box.y < reach.down);
    classes.toggle("at-bottom", 100 - (box.y + height) < reach.down);
  }

  // How near the canvas's edge an item's edge must be, in percent across and down, for
  // the canvas to clip the handles on it: half a handle, whose size app.css sets.
  function handleReach() {
    const size = parseFloat(getComputedStyle(ui.selection).getPropertyValue("--handle")) || 0;
    return {
      across: (size / 2 / ui.canvas.clientWidth) * 100,
      down: (size / 2 / ui.canvas.clientHeight) * 100,
    };
  }

  // ------------------------------------------------------------ pointer handling

  // What a press lands on: a block (its index), the photo where no block is, or nothing,
  // on the canvas around the photo's box.
  function pressedItem(target) {
    const element = target.closest("[data-block]");
    if (element) return Number(element.dataset.block);
    return target.closest("[data-editor-photo-box]") ? PHOTO : NONE;
  }

  // A press on a block, or on the photo, chooses it and drags it; a press on a handle
  // resizes the chosen item; a press on the canvas around the photo lets the choice go.
  function onCanvasPointerDown(event) {
    if (event.button !== 0) return;
    const handle = event.target.closest("[data-editor-handle]");
    const target = handle ? editor.selected : pressedItem(event.target);
    if (target !== editor.selected) select(target);
    const item = selectedItem();
    if (!item || !item.element) return;
    startGesture(event, item, handle);
  }

  // Drag the item, or resize it from `handle`, while the pointer that pressed it moves,
  // and count the change when it lets go. A press that only chose the item changes nothing.
  function startGesture(event, item, handle) {
    const target = handle || item.element;
    const { box, from, settle } = workingBox(item);
    from.height = heightOf(item.kind, box, item.element);
    const canvas = ui.canvas.getBoundingClientRect();
    target.setPointerCapture(event.pointerId);

    // Only the pointer that started the gesture moves the item or ends the gesture.
    const onMove = (move) => {
      if (move.pointerId !== event.pointerId) return;
      const dx = ((move.clientX - event.clientX) / canvas.width) * 100;
      const dy = ((move.clientY - event.clientY) / canvas.height) * 100;
      if (handle) {
        resizeItem(item.kind, box, from, handle.dataset.editorHandle, dx, dy);
        placeBox(item.element, box, item.kind);
        keepInside(item.kind, box, item.element);
      } else {
        moveItem(item.kind, box, from, dx, dy, from.height);
      }
      placeBox(item.element, box, item.kind);
      drawSelection();
    };
    const onEnd = (end) => {
      if (end.pointerId !== event.pointerId) return;
      target.removeEventListener("pointermove", onMove);
      target.removeEventListener("pointerup", onEnd);
      target.removeEventListener("pointercancel", onEnd);
      if (settle()) markStale();
      drawSelection();
      syncPanel();
    };
    target.addEventListener("pointermove", onMove);
    target.addEventListener("pointerup", onEnd);
    target.addEventListener("pointercancel", onEnd);
  }

  // ------------------------------------------------------------ keyboard

  // With something chosen, the arrow keys nudge it (further with Shift), Delete or
  // Backspace removes a shade, a text block or an image, and Escape lets the choice go.
  // Keys pressed in a field, or with a modifier the browser uses, are left alone.
  function onKeyDown(event) {
    if (editor.selected === NONE || event.ctrlKey || event.metaKey || event.altKey) return;
    const focused = document.activeElement;
    if (focused && focused.matches("input, select, textarea")) return;
    const arrow = ARROWS[event.key];
    if (arrow) {
      event.preventDefault();
      const step = event.shiftKey ? SHIFT_NUDGE : NUDGE;
      nudge(arrow[0] * step, arrow[1] * step);
    } else if (event.key === "Delete" || event.key === "Backspace") {
      const block = selectedBlock();
      if (!block || !DELETABLE.includes(block.kind)) return;
      event.preventDefault();
      deleteSelected();
    } else if (event.key === "Escape") {
      select(NONE);
    }
  }

  // The chosen item moved by a nudge, within the edges a drag keeps to.
  function nudge(dx, dy) {
    const item = selectedItem();
    if (!item || !item.element) return;
    const { box, from, settle } = workingBox(item);
    moveItem(item.kind, box, from, dx, dy, heightOf(item.kind, box, item.element));
    if (!settle()) return;
    placeBox(item.element, box, item.kind);
    markStale();
    drawSelection();
  }

  // ------------------------------------------------------------ the panel

  function kindGroup(kind) {
    return isText(kind) ? "text" : kind;
  }

  function radios(name) {
    return ui.root.querySelectorAll(`input[type="radio"][name="${name}"]`);
  }

  function checkRadio(name, value) {
    radios(name).forEach((input) => {
      input.checked = input.value === value;
    });
  }

  // A row of swatches: the chosen one, the brand's colour for the role, and the
  // chosen colour's name under the row.
  function syncSwatches(group, value, brandHex) {
    const box = ui.root.querySelector(`[data-swatches="${group}"]`);
    if (!box) return;
    checkRadio(group, value);
    const brand = box.querySelector("[data-brand-swatch]");
    if (brand) brand.style.setProperty("--swatch", brandHex);
    const chosen = box.querySelector("input:checked");
    setText(box.querySelector("[data-swatch-name]"), chosen ? chosen.closest("label").title : "");
  }

  // The panel shows the chosen item's own controls under its name: the words' face,
  // weight, italics, size, alignment and colour (and a text block's words), a shade's
  // colour and opacity, an image's fit, aspect and opacity, the photo's fit, position and
  // the canvas colour, then Bring forward, Send back and Delete for the blocks that have
  // them. The post mode and the logo width are always there. A part is shown for the
  // item's group (data-editor-for: the words share one) or for its own kind
  // (data-editor-for-kind).
  function syncPanel() {
    const item = selectedItem();
    const group = item ? kindGroup(item.kind) : "";
    setText(ui.blockName, item ? KIND_NAMES[item.kind] : "");
    ui.blockName.hidden = !item;
    ui.noBlock.hidden = Boolean(item);
    ui.blockParts.forEach((part) => {
      part.hidden = !part.dataset.editorFor.split(" ").includes(group);
    });
    ui.kindParts.forEach((part) => {
      part.hidden = !(item && part.dataset.editorForKind.split(" ").includes(item.kind));
    });
    if (group === "text") syncText(item.box);
    if (group === "text" || group === "shade") {
      syncSwatches("colour", item.box.colour, roleHex(ROLES[item.kind]));
    }
    if (group === "shade") ui.opacity.value = String(item.box.opacity);
    if (group === "image") syncImage(item.box);
    if (item && ORDERABLE.includes(item.kind)) {
      ui.forward.disabled = editor.selected >= editor.layout.blocks.length - 1;
      ui.back.disabled = editor.selected <= 0;
    }

    const layout = editor.layout;
    checkRadio("photo_fit", layout.photo_fit);
    syncSwatches("background", layout.background, roleHex("background"));
    checkRadio("mode", editor.mode);
    const logo = firstLogo();
    ui.logoWidth.value = logo ? String(Math.round(logo.w)) : "";
    ui.logoWidth.disabled = !logo;
  }

  // A headline's, a subline's or a text block's face, weight and italics, then its size
  // and alignment, and a text block's words. A face without an italic file leaves the
  // toggle off and disabled, with a hint. The words are set only when they differ, so the
  // field keeps its caret while the designer types in it.
  function syncText(block) {
    const face = faceOf(block);
    const hasItalic = Boolean(face && face.italic_url);
    ui.font.value = face ? face.name : "";
    ui.weight.value = String(block.weight || 0);
    ui.italic.checked = Boolean(block.italic) && hasItalic;
    ui.italic.disabled = !hasItalic;
    ui.noItalic.hidden = hasItalic;
    ui.size.value = String(block.size_px || DEFAULT_PX[block.kind]);
    checkRadio("align", block.align);
    if (block.kind === "text" && ui.text.value !== block.text) ui.text.value = block.text;
  }

  // An image's fit, whether it keeps its aspect, and its opacity.
  function syncImage(block) {
    checkRadio("image_fit", block.fit);
    ui.keepAspect.checked = Boolean(block.keep_aspect);
    ui.imageOpacity.value = String(block.opacity);
  }

  function onRadio(name, apply) {
    radios(name).forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) apply(input.value);
      });
    });
  }

  // The canvas changed after a preview, so Use this layout would finish an arrangement
  // that is no longer on it: the button waits for the next preview, and a line says so.
  // A change by hand after an Ask also ends what Undo offers, since bringing back the layout
  // from before the Ask would throw that change away; the change an answer or Undo itself
  // loads (`editor.loading`) keeps it.
  function markStale() {
    editor.changes += 1;
    if (!editor.loading && editor.undo) {
      editor.undo = null;
      ui.askUndo.hidden = true;
    }
    if (ui.finishButton.disabled) return;
    ui.finishButton.disabled = true;
    ui.stale.hidden = false;
  }

  function updateSelected(changes) {
    const block = selectedBlock();
    if (!block) return;
    Object.assign(block, changes);
    markStale();
    redraw();
    syncPanel();
  }

  // A number field applies as it is typed once the number is in range; when the
  // field is left, the number is brought into range, or the one in use comes back.
  function onNumber(input, low, high, apply) {
    input.addEventListener("input", () => {
      const value = Number(input.value);
      if (input.value !== "" && value >= low && value <= high) apply(value);
    });
    input.addEventListener("change", () => {
      if (input.value !== "" && Number.isFinite(Number(input.value))) {
        apply(within(Number(input.value), low, high));
      }
      syncPanel();
    });
  }

  function setSize(px) {
    const block = selectedBlock();
    if (!block || kindGroup(block.kind) !== "text") return;
    block.size_px = Math.round(px);
    markStale();
    redraw();
  }

  // The chosen words set in another face. The kit's family is kept as "", as the renderer
  // reads it, and a face without an italic file turns italics off. The face loads before
  // the words are drawn again, so they are measured in it.
  async function setFont(name) {
    const block = selectedBlock();
    if (!block || !isText(block.kind)) return;
    const face = faceNamed(name);
    block.font = face && face !== editor.data.faces[0] ? face.name : "";
    const chosen = faceOf(block);
    if (!chosen || !chosen.italic_url) block.italic = false;
    markStale();
    await faceLoaded(block);
    redraw();
    syncPanel();
  }

  // The chosen words in italics or upright. The italic file loads before the words are
  // drawn again, so they are measured in it.
  async function setItalic(italic) {
    const block = selectedBlock();
    if (!block || !isText(block.kind)) return;
    block.italic = italic;
    markStale();
    await faceLoaded(block);
    redraw();
    syncPanel();
  }

  // The logo widens or narrows from its left edge, moving left when it would cross
  // the margin, and never narrower than the renderer allows.
  function setLogoWidth(width) {
    const logo = firstLogo();
    if (!logo) return;
    logo.w = width;
    logo.x = Math.min(logo.x, FAR - width);
    markStale();
    redraw();
  }

  // The photo moves the way the arrow points. Under cover it overflows its box, so its
  // position runs against the move; under contain it runs with it.
  function pan(direction) {
    const step = PAN_DIRECTIONS[direction];
    if (!step) return;
    const layout = editor.layout;
    const move = (layout.photo_fit === "cover" ? -1 : 1) * PAN_STEP;
    const x = within(layout.photo_offset_x + step[0] * move, -PAN_LIMIT, PAN_LIMIT);
    const y = within(layout.photo_offset_y + step[1] * move, -PAN_LIMIT, PAN_LIMIT);
    if (x === layout.photo_offset_x && y === layout.photo_offset_y) return; // at the limit
    layout.photo_offset_x = x;
    layout.photo_offset_y = y;
    markStale();
    drawPhoto();
  }

  // The photo back over the whole canvas, centred in it.
  function fillCanvas() {
    const layout = editor.layout;
    if (!layout.photo && !layout.photo_offset_x && !layout.photo_offset_y) return;
    layout.photo = null;
    layout.photo_offset_x = 0;
    layout.photo_offset_y = 0;
    markStale();
    drawPhoto();
    drawSelection();
  }

  // The list holds as many blocks as a layout keeps, so nothing more can be added.
  function isFull() {
    return editor.layout.blocks.length >= MAX_BLOCKS;
  }

  // The three Add buttons wait while the list is full, and the picker closes.
  function syncAddButtons() {
    const full = isFull();
    [ui.addShade, ui.addText, ui.addImage].forEach((button) => {
      button.disabled = full;
    });
    if (full) closePicker();
  }

  // A new shade goes behind the words and the logo, as it always has: into the list just
  // before the first of them, so it is drawn before them and they sit on top.
  function addShade() {
    if (isFull()) return;
    const blocks = editor.layout.blocks;
    const words = blocks.findIndex((block) => isText(block.kind) || block.kind === "logo");
    const index = words < 0 ? blocks.length : words;
    blocks.splice(index, 0, { ...NEW_SHADE });
    markStale();
    drawCanvas();
    select(index);
  }

  // A new line of text, on top of the list, in the middle of the canvas once its height
  // is known, and chosen, with its words selected in the Words field: typing replaces them
  // at once, and Backspace clears them there rather than deleting the block.
  function addText() {
    if (isFull()) return;
    const block = { ...NEW_TEXT };
    editor.layout.blocks.push(block);
    const index = editor.layout.blocks.length - 1;
    markStale();
    drawCanvas();
    const height = heightOf(block.kind, block, blockElement(index));
    block.y = within((100 - height) / 2, MARGIN, FAR - height);
    redraw();
    select(index);
    ui.text.focus();
    ui.text.select();
  }

  // An upload placed as a new image on top of the list: a third of the canvas wide, its
  // height from the picture's proportions (a picture too tall for that fits the canvas's
  // height instead, and one too wide is given the guardrails' smallest height, its picture
  // shown whole inside), centred and chosen.
  function placeImage(upload) {
    if (isFull()) return;
    const size = editor.data.post_size;
    const tall = upload.width > 0 && upload.height > 0 ? upload.height / upload.width : 1;
    let w = NEW_IMAGE_WIDTH;
    let h = w * (size.width / size.height) * tall;
    if (h > 100) {
      w = Math.max(MIN_IMAGE, (w * 100) / h);
      h = 100;
    }
    h = Math.max(h, MIN_IMAGE);
    const block = {
      ...NEW_IMAGE,
      x: (100 - w) / 2,
      y: (100 - h) / 2,
      w,
      h,
      image_path: mediaPath(upload.url),
    };
    editor.layout.blocks.push(block);
    markStale();
    drawCanvas();
    select(editor.layout.blocks.length - 1);
  }

  function removeShade() {
    const block = selectedBlock();
    if (!block || block.kind !== "shade") return;
    editor.layout.blocks.splice(editor.selected, 1);
    editor.selected = NONE;
    markStale();
    drawCanvas();
    syncPanel();
  }

  // The chosen shade, text block or image taken off the canvas. The headline, the subline
  // and the logo stay.
  function deleteSelected() {
    const block = selectedBlock();
    if (!block || !DELETABLE.includes(block.kind)) return;
    editor.layout.blocks.splice(editor.selected, 1);
    editor.selected = NONE;
    markStale();
    drawCanvas();
    syncPanel();
  }

  // The chosen block swapped with its neighbour in the list: forward (1) moves it later,
  // so it is drawn over that neighbour; back (-1) earlier, so under it. The choice stays
  // on the block.
  function moveInList(step) {
    const blocks = editor.layout.blocks;
    const from = editor.selected;
    const to = from + step;
    const block = selectedBlock();
    if (!block || !ORDERABLE.includes(block.kind) || to < 0 || to >= blocks.length) return;
    [blocks[from], blocks[to]] = [blocks[to], blocks[from]];
    markStale();
    drawCanvas();
    select(to);
  }

  // An image's opacity as it is typed, without drawing the panel again under the caret.
  function setImageOpacity(opacity) {
    const block = selectedBlock();
    if (!block || block.kind !== "image") return;
    block.opacity = within(opacity, 0, 1);
    markStale();
    redraw();
  }

  // ------------------------------------------------------------ the uploads picker

  // Add image opens the picker under the Add buttons, with the brand's uploads listed
  // afresh each time, and closes it again; Add image says which, for a screen reader.
  function openPicker() {
    ui.uploads.hidden = false;
    ui.addImage.setAttribute("aria-expanded", "true");
    loadUploads();
  }

  function closePicker() {
    ui.uploads.hidden = true;
    ui.addImage.setAttribute("aria-expanded", "false");
  }

  function togglePicker() {
    if (ui.uploads.hidden) openPicker();
    else closePicker();
  }

  // The picker's two lines: what went wrong, and what is happening; each hidden when empty.
  function showUploadError(message) {
    setText(ui.uploadError, message);
    ui.uploadError.hidden = !message;
  }

  function setUploadStatus(message) {
    setText(ui.uploadStatus, message);
    ui.uploadStatus.hidden = !message;
  }

  // The brand's uploads, newest first, as thumbnails with their names, each a button that
  // places it.
  function showUploads(uploads) {
    editor.uploads = uploads;
    ui.uploadsList.replaceChildren(
      ...uploads.map((upload) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "upload-pick";
        button.dataset.uploadId = upload.id;
        const thumb = document.createElement("span");
        thumb.className = "upload-thumb";
        const image = document.createElement("img");
        image.src = upload.url;
        image.alt = "";
        image.loading = "lazy";
        thumb.append(image);
        const name = document.createElement("span");
        name.className = "upload-name";
        name.textContent = upload.name || UNTITLED_UPLOAD;
        button.append(thumb, name);
        item.append(button);
        return item;
      }),
    );
    ui.uploadsEmpty.hidden = uploads.length > 0;
  }

  // The brand's uploads from the server, newest first; a line says so when they cannot be
  // listed.
  async function loadUploads() {
    showUploadError("");
    try {
      const response = await fetch("/api/uploads");
      if (!response.ok) throw new Error(String(response.status));
      const result = await response.json();
      showUploads(Array.isArray(result.uploads) ? result.uploads : []);
    } catch {
      showUploadError(UPLOADS_FAILED);
    }
  }

  // A file sent to the brand's shelf: the server's entry for it (id, url, name, width and
  // height), or { error } with the reason when the studio cannot use the file. Throws when
  // the server cannot be reached. Add image and the Ask panel both send files this way.
  async function sendUpload(file) {
    const form = new FormData();
    form.append("image", file);
    const response = await fetch("/editor/upload", { method: "POST", body: form });
    const result = await response.json().catch(() => ({}));
    if (response.ok) return result;
    const reason = typeof result.error === "string" && result.error;
    return { error: reason || UPLOAD_FAILED };
  }

  // The chosen file sent to the brand's shelf, then placed. A file the studio cannot use
  // comes back with the reason, shown under the field.
  async function uploadImage() {
    const file = ui.uploadFile.files[0];
    if (!file || isFull()) return;
    ui.uploadButton.disabled = true;
    showUploadError("");
    setUploadStatus(UPLOADING);
    try {
      const result = await sendUpload(file);
      if (result.error) {
        showUploadError(result.error);
        return;
      }
      ui.uploadFile.value = "";
      placeImage(result);
      closePicker();
    } catch {
      showUploadError(UPLOAD_FAILED); // offline, or the server went away
    } finally {
      setUploadStatus("");
      ui.uploadButton.disabled = !ui.uploadFile.files.length;
    }
  }

  // A thumbnail pressed in the picker places its upload.
  function onUploadPick(event) {
    const button = event.target.closest("[data-upload-id]");
    if (!button) return;
    const upload = editor.uploads.find((entry) => entry.id === button.dataset.uploadId);
    if (!upload) return;
    placeImage(upload);
    closePicker();
  }

  function wirePanel() {
    ui.root.querySelectorAll("[data-editor-word]").forEach((input) => {
      input.addEventListener("input", () => {
        markStale();
        redraw();
      });
    });
    ui.font.addEventListener("change", () => setFont(ui.font.value));
    ui.weight.addEventListener("change", () => {
      updateSelected({ weight: Number(ui.weight.value) });
    });
    ui.italic.addEventListener("change", () => setItalic(ui.italic.checked));
    onNumber(ui.size, MIN_PX, MAX_PX, setSize);
    onRadio("align", (value) => updateSelected({ align: value }));
    onRadio("colour", (value) => updateSelected({ colour: value }));
    ui.opacity.addEventListener("input", () => {
      updateSelected({ opacity: within(Number(ui.opacity.value), 0, 1) });
    });
    ui.removeShade.addEventListener("click", removeShade);
    ui.addShade.addEventListener("click", addShade);
    // v5: the designer's own text and images, and the order of the blocks.
    ui.text.addEventListener("input", () => {
      const block = selectedBlock();
      if (!block || block.kind !== "text") return;
      block.text = ui.text.value.slice(0, MAX_TEXT_CHARS);
      markStale();
      redraw();
    });
    ui.addText.addEventListener("click", addText);
    ui.addImage.addEventListener("click", togglePicker);
    ui.uploadFile.addEventListener("change", () => {
      ui.uploadButton.disabled = !ui.uploadFile.files.length;
      showUploadError("");
    });
    ui.uploadButton.addEventListener("click", uploadImage);
    ui.uploadsList.addEventListener("click", onUploadPick);
    onRadio("image_fit", (value) => updateSelected({ fit: value }));
    ui.keepAspect.addEventListener("change", () => {
      updateSelected({ keep_aspect: ui.keepAspect.checked });
    });
    onNumber(ui.imageOpacity, 0, 1, setImageOpacity);
    ui.forward.addEventListener("click", () => moveInList(1));
    ui.back.addEventListener("click", () => moveInList(-1));
    ui.deleteButton.addEventListener("click", deleteSelected);
    onRadio("photo_fit", (value) => {
      editor.layout.photo_fit = value;
      markStale();
      drawPhoto();
    });
    ui.root.querySelectorAll("[data-pan]").forEach((button) => {
      button.addEventListener("click", () => pan(button.dataset.pan));
    });
    ui.fill.addEventListener("click", fillCanvas);
    onRadio("background", (value) => {
      editor.layout.background = value;
      markStale();
      drawPhoto();
      syncPanel();
    });
    onRadio("mode", (value) => {
      editor.mode = value;
      markStale();
      redraw();
      syncPanel();
    });
    onNumber(ui.logoWidth, MIN_LOGO, FAR - MARGIN, setLogoWidth);
    ui.previewButton.addEventListener("click", preview);
    // v5: the Ask panel. Enter in the field applies, as Apply does. Another file chosen is
    // sent to the shelf afresh.
    ui.askForm.addEventListener("submit", ask);
    ui.askUndo.addEventListener("click", undoAsk);
    ui.askFile.addEventListener("change", () => {
      editor.askUpload = null;
    });
  }

  // ------------------------------------------------------------ preview

  function showError(message, runId) {
    ui.error.replaceChildren();
    ui.error.hidden = !message;
    if (!message) return;
    ui.error.append(message);
    if (runId) {
      const link = document.createElement("a");
      link.href = `/runs/${runId}`;
      link.textContent = SEE_THE_RUN;
      ui.error.append(" ", link);
    }
  }

  // The render beside the canvas with what the renderer changed or found, one line
  // each, and Use this layout pointed at its candidate: ready to finish it while the
  // canvas is as it was sent (`current`), or waiting for the next preview.
  function showPreview(result, current) {
    ui.previewImage.src = result.image_url;
    const lines = Array.isArray(result.adjustments) ? result.adjustments : [];
    ui.adjustments.replaceChildren(
      ...lines.map((line) => {
        const item = document.createElement("li");
        item.textContent = line;
        return item;
      }),
    );
    ui.adjustments.hidden = lines.length === 0;
    ui.preview.hidden = false;
    ui.finishForm.action = `/sessions/${editor.data.session_id}/finish/${result.candidate_id}`;
    ui.finishButton.disabled = !current;
    ui.stale.hidden = current;
  }

  function errorText(result) {
    if (typeof result.error === "string" && result.error) return result.error;
    if (typeof result.detail === "string" && result.detail) return result.detail;
    return PREVIEW_FAILED;
  }

  // Use this layout stays off while the preview renders, and after one that fails: only
  // a preview that comes back points it at a candidate.
  async function preview() {
    const sent = editor.changes;
    ui.previewButton.disabled = true;
    ui.finishButton.disabled = true;
    ui.stale.hidden = true;
    showError("");
    setText(ui.status, RENDERING);
    try {
      const response = await fetch(`/sessions/${editor.data.session_id}/editor/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sample_id: editor.data.sample_id,
          headline: wordsOf("headline"),
          subline: wordsOf("subline"),
          mode: editor.mode,
          layout: editor.layout,
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (response.ok) showPreview(result, editor.changes === sent);
      else showError(errorText(result), result.run_id);
    } catch {
      showError(PREVIEW_FAILED); // offline, or the server went away
    } finally {
      ui.previewButton.disabled = false;
      setText(ui.status, "");
    }
  }

  // ------------------------------------------------------------ the Ask panel (v5)

  // The panel's two lines: what the editor did or asks, and what went wrong; each hidden
  // when empty.
  function showAskLine(message) {
    setText(ui.askLine, message);
    ui.askLine.hidden = !message;
  }

  function showAskError(message) {
    setText(ui.askError, message);
    ui.askError.hidden = !message;
  }

  // While the editor answers, the panel waits: nothing in it can be pressed or typed in.
  function setAskBusy(busy) {
    [ui.askRequest, ui.askFile, ui.askApply, ui.askUndo].forEach((control) => {
      control.disabled = busy;
    });
  }

  // A whole arrangement loaded into the canvas: the editor's answer, or the one Undo brings
  // back. It keeps to the renderer's rules as the starting one does; the chosen item stays
  // chosen while a block of the same kind is at its place; and the canvas counts as changed,
  // so Use this layout waits for the next preview. The faces it uses load before its words
  // are measured again.
  async function loadLayout(layout) {
    const chosen = selectedItem();
    editor.layout = layout;
    editor.layout.blocks.forEach(guard);
    guardPhoto(editor.layout.photo);
    if (!paletteHex(editor.layout.background)) editor.layout.background = "";
    const block = editor.layout.blocks[editor.selected];
    const same = editor.selected === PHOTO || (block && chosen && block.kind === chosen.kind);
    editor.selected = same ? editor.selected : NONE;
    editor.loading = true;
    try {
      markStale();
      drawCanvas();
      syncPanel();
    } finally {
      editor.loading = false;
    }
    await facesLoaded();
    redraw();
  }

  // The line after edits: the editor's summary, then each edit that was dropped or changed.
  function askSummary(result) {
    const dropped = Array.isArray(result.dropped) ? result.dropped : [];
    const summary = typeof result.summary === "string" ? result.summary : "";
    return [summary, ...dropped].filter(Boolean).join(" ") || ASK_CHANGED;
  }

  // The Headline and Subline fields set from the editor's answer, or from what Undo brings
  // back; a word the source does not give stays as it is.
  function setWords(words) {
    ["headline", "subline"].forEach((kind) => {
      if (ui.words[kind] && typeof words[kind] === "string") ui.words[kind].value = words[kind];
    });
  }

  // The chosen file's id on the brand's shelf: the one it was given when it was sent before,
  // so answering a question does not send it again, or a new one. Gives back { error } with
  // the reason when the studio cannot use the file or the server cannot be reached.
  async function askFileUpload(file) {
    const last = editor.askUpload;
    if (last && last.file === file) return { id: last.id };
    let upload;
    try {
      upload = await sendUpload(file);
    } catch {
      return { error: UPLOAD_FAILED }; // offline, or the server went away
    }
    if (!upload.error) editor.askUpload = { file, id: upload.id };
    return upload;
  }

  // Apply: the chosen file, when there is one, goes to the brand's shelf first, then the
  // request, the canvas's arrangement and the words and mode go to the editor. Its edits
  // load into the canvas, new words into their fields, the arrangement and the words before
  // them kept for Undo, and the field clears; a question shows in the line, the request kept
  // in the field to answer. An answer to a canvas that has changed since is not loaded.
  // Nothing is saved.
  async function ask(event) {
    event.preventDefault();
    const request = ui.askRequest.value.trim();
    if (!request) {
      ui.askRequest.focus();
      return;
    }
    let question = false;
    setAskBusy(true);
    showAskError("");
    showAskLine(ASK_WORKING);
    try {
      let uploadId = null;
      const file = ui.askFile.files[0];
      if (file) {
        const upload = await askFileUpload(file);
        if (upload.error) {
          showAskLine("");
          showAskError(upload.error);
          return;
        }
        uploadId = upload.id;
      }
      // As for a preview: a change made while the editor answers means its answer is to a
      // layout that is no longer on the canvas.
      const sent = editor.changes;
      const response = await fetch(`/sessions/${editor.data.session_id}/editor/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request,
          layout: editor.layout,
          upload_id: uploadId,
          sample: editor.data.sample_id,
          from: new URLSearchParams(window.location.search).get("from"),
          headline: wordsOf("headline"),
          subline: wordsOf("subline"),
          mode: editor.mode,
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        const reason = typeof result.error === "string" && result.error;
        showAskLine("");
        showAskError(reason || ASK_FAILED);
        return;
      }
      if (!result.usable) {
        question = true;
        showAskLine(result.question);
        return;
      }
      if (editor.changes !== sent) {
        showAskLine(ASK_STALE);
        return;
      }
      const before = {
        layout: structuredClone(editor.layout),
        headline: wordsOf("headline"),
        subline: wordsOf("subline"),
      };
      if (result.words) setWords(result.words);
      await loadLayout(result.layout);
      editor.undo = before;
      ui.askUndo.hidden = false;
      ui.askRequest.value = "";
      ui.askFile.value = "";
      editor.askUpload = null;
      showAskLine(askSummary(result));
    } catch {
      showAskLine("");
      showAskError(ASK_FAILED); // offline, or the server went away
    } finally {
      setAskBusy(false);
      // A question is answered in the field, so the caret goes back to it.
      if (question) ui.askRequest.focus();
    }
  }

  // Undo puts back the arrangement and the words from before the last change the editor
  // made, then hides.
  async function undoAsk() {
    const undo = editor.undo;
    if (!undo) return;
    editor.undo = null;
    ui.askUndo.hidden = true;
    showAskLine("");
    showAskError("");
    setWords(undo);
    await loadLayout(undo.layout);
  }

  // ------------------------------------------------------------ start

  function findElements(root) {
    ui.root = root;
    ui.words = {
      headline: root.querySelector('[data-editor-word="headline"]'),
      subline: root.querySelector('[data-editor-word="subline"]'),
    };
    ui.canvas = root.querySelector("[data-editor-canvas]");
    ui.photoBox = root.querySelector("[data-editor-photo-box]");
    ui.photo = root.querySelector("[data-editor-photo]");
    ui.selection = root.querySelector("[data-editor-selection]");
    ui.handles = Array.from(root.querySelectorAll("[data-editor-handle]"));
    ui.blockName = root.querySelector("[data-editor-block-name]");
    ui.noBlock = root.querySelector("[data-editor-no-block]");
    ui.blockParts = Array.from(root.querySelectorAll("[data-editor-for]"));
    ui.kindParts = Array.from(root.querySelectorAll("[data-editor-for-kind]"));
    ui.font = root.querySelector("[data-editor-font]");
    ui.weight = root.querySelector("[data-editor-weight]");
    ui.italic = root.querySelector("[data-editor-italic]");
    ui.noItalic = root.querySelector("[data-editor-no-italic]");
    ui.size = root.querySelector("[data-editor-size]");
    ui.opacity = root.querySelector("[data-editor-opacity]");
    ui.removeShade = root.querySelector("[data-editor-remove-shade]");
    ui.addShade = root.querySelector("[data-editor-add-shade]");
    ui.text = root.querySelector("[data-editor-text]");
    ui.keepAspect = root.querySelector("[data-editor-keep-aspect]");
    ui.imageOpacity = root.querySelector("[data-editor-image-opacity]");
    ui.forward = root.querySelector("[data-editor-forward]");
    ui.back = root.querySelector("[data-editor-back]");
    ui.deleteButton = root.querySelector("[data-editor-delete]");
    ui.addText = root.querySelector("[data-editor-add-text]");
    ui.addImage = root.querySelector("[data-editor-add-image]");
    ui.uploads = root.querySelector("[data-editor-uploads]");
    ui.uploadFile = root.querySelector("[data-editor-upload-file]");
    ui.uploadButton = root.querySelector("[data-editor-upload-button]");
    ui.uploadStatus = root.querySelector("[data-editor-upload-status]");
    ui.uploadError = root.querySelector("[data-editor-upload-error]");
    ui.uploadsList = root.querySelector("[data-editor-uploads-list]");
    ui.uploadsEmpty = root.querySelector("[data-editor-uploads-empty]");
    ui.fill = root.querySelector("[data-editor-fill]");
    ui.logoWidth = root.querySelector("[data-editor-logo-width]");
    ui.preview = root.querySelector("[data-editor-preview]");
    ui.previewImage = root.querySelector("[data-editor-preview-image]");
    ui.adjustments = root.querySelector("[data-editor-adjustments]");
    ui.previewButton = root.querySelector("[data-editor-preview-button]");
    ui.finishForm = root.querySelector("[data-editor-finish]");
    ui.finishButton = root.querySelector("[data-editor-finish-button]");
    ui.stale = root.querySelector("[data-editor-stale]");
    ui.status = root.querySelector("[data-editor-status]");
    ui.error = root.querySelector("[data-editor-error]");
    ui.askForm = root.querySelector("[data-editor-ask-form]");
    ui.askRequest = root.querySelector("[data-editor-ask-request]");
    ui.askFile = root.querySelector("[data-editor-ask-file]");
    ui.askApply = root.querySelector("[data-editor-ask-apply]");
    ui.askUndo = root.querySelector("[data-editor-ask-undo]");
    ui.askLine = root.querySelector("[data-editor-ask-line]");
    ui.askError = root.querySelector("[data-editor-ask-error]");
  }

  // The kit's headline tracking and post size, as the renderer's page sets them. The
  // faces and weights are set on each block.
  function applyKit() {
    const style = ui.canvas.style;
    style.setProperty("--headline-tracking", editor.data.typography.headline_tracking);
    style.setProperty("--post-width", String(editor.data.post_size.width));
    style.aspectRatio = `${editor.data.post_size.width} / ${editor.data.post_size.height}`;
  }

  async function initEditor() {
    const root = document.querySelector("[data-editor]");
    const source = document.getElementById("editor-data");
    if (!root || !source) return;
    const data = JSON.parse(source.textContent);
    editor.data = data;
    editor.layout = data.layout;
    editor.mode = data.mode;
    findElements(root);
    declareFaces();
    applyKit();

    editor.layout.blocks.forEach(guard);
    guardPhoto(editor.layout.photo);
    if (!paletteHex(editor.layout.background)) editor.layout.background = "";
    editor.selected = editor.layout.blocks.findIndex((block) => block.kind === "headline");
    drawCanvas();
    syncPanel();
    wirePanel();
    ui.canvas.addEventListener("pointerdown", onCanvasPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", drawSelection);
    // The words are measured in their faces: once those have loaded, the subline moves
    // clear of the headline, then the first margin pass keeps the words inside.
    await facesLoaded();
    editor.fontLoaded = true;
    clearSubline();
    redraw();
    document.fonts.addEventListener("loadingdone", redraw);
  }

  initEditor();
})();
