/**
 * Tiny DOM builder.
 *
 * Every view in this app is built with `h()` instead of innerHTML templates:
 * text always goes through text nodes, so user content and tool output can
 * never be parsed as markup. The only place HTML is injected is the markdown
 * renderer, which produces it from escaped input.
 */

const TAG_PATTERN = /^([a-z0-9-]+)((?:\.[\w-]+)*)$/i;

/**
 * @param {string} spec - tag name, optionally with classes: `"div.step.step--tool"`.
 * @param {object} [props] - `class`, `text`, `html`, `dataset`, `style`, `on`, or attributes.
 * @param {...(Node|string|null|undefined|Array)} children
 */
export function h(spec, props = {}, ...children) {
  const match = TAG_PATTERN.exec(spec);
  if (!match) throw new Error(`Invalid element spec: ${spec}`);
  const node = document.createElement(match[1]);
  const specClasses = match[2] ? match[2].slice(1).split(".") : [];
  if (specClasses.length) node.classList.add(...specClasses);

  for (const [key, value] of Object.entries(props ?? {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.classList.add(...String(value).split(" ").filter(Boolean));
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") Object.assign(node.style, value);
    else if (key === "on") for (const [type, fn] of Object.entries(value)) node.addEventListener(type, fn);
    else node.setAttribute(key, value === true ? "" : String(value));
  }

  append(node, children);
  return node;
}

/** Append children of any shape, skipping nullish entries. */
export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

/** Replace a node's children in one pass. */
export function replace(parent, ...children) {
  parent.replaceChildren();
  return append(parent, children);
}

/** Inline SVG icon from a 24x24 path definition. */
export function icon(path, { size = 16, className = "icon" } = {}) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", className);
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  shape.setAttribute("d", path);
  svg.append(shape);
  return svg;
}

export const $ = (selector, scope = document) => scope.querySelector(selector);
export const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];

/** Trailing-edge debounce. */
export function debounce(fn, delay = 120) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

/** Run at most once per animation frame - for high-frequency stream updates. */
export function onFrame(fn) {
  let queued = false;
  return (...args) => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      fn(...args);
    });
  };
}
