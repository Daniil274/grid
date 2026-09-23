/**
 * Just enough DOM for the web chat's views to be built and inspected in Node:
 * elements, text nodes, classes, datasets, attributes and click events.
 * Not a browser - no layout, no selectors, no parsing of `innerHTML`.
 */

class FakeNode {
  constructor() {
    this.parentNode = null;
    this.childNodes = [];
  }

  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === "string" ? new FakeText(node) : node;
      child.remove();
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }

  replaceChildren(...nodes) {
    for (const child of [...this.childNodes]) child.remove();
    this.append(...nodes);
  }

  replaceWith(node) {
    const parent = this.parentNode;
    if (!parent) return;
    node.remove();
    const index = parent.childNodes.indexOf(this);
    parent.childNodes.splice(index, 1, node);
    node.parentNode = parent;
    this.parentNode = null;
  }

  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.childNodes;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }

  get textContent() {
    return this.childNodes.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.replaceChildren();
    if (value !== "") this.append(new FakeText(String(value)));
  }
}

class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.data = text;
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

class FakeClassList {
  constructor() {
    this.names = new Set();
  }

  add(...names) {
    for (const name of names) this.names.add(name);
  }

  remove(...names) {
    for (const name of names) this.names.delete(name);
  }

  contains(name) {
    return this.names.has(name);
  }

  toggle(name, force = !this.names.has(name)) {
    if (force) this.names.add(name);
    else this.names.delete(name);
    return force;
  }
}

class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tagName = tag.toUpperCase();
    this.classList = new FakeClassList();
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.title = "";
    this.html = null;
  }

  setAttribute(name, value) {
    if (name === "class") this.classList.add(...String(value).split(" ").filter(Boolean));
    else this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  addEventListener(type, fn) {
    (this.listeners[type] ??= []).push(fn);
  }

  click() {
    for (const fn of this.listeners.click ?? []) fn({ target: this });
  }

  set innerHTML(value) {
    this.replaceChildren();
    this.html = value;
  }

  get innerHTML() {
    return this.html ?? "";
  }

  /** Depth-first search by class name, the one query these tests need. */
  findAll(className) {
    const found = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (!(child instanceof FakeElement)) continue;
        if (child.classList.contains(className)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }

  find(className) {
    return this.findAll(className)[0] ?? null;
  }

  /** Direct children carrying a class. */
  childrenWith(className) {
    return this.childNodes.filter((child) => child instanceof FakeElement && child.classList.contains(className));
  }
}

export function installFakeDom() {
  globalThis.Node = FakeNode;
  globalThis.document = {
    createElement: (tag) => new FakeElement(tag),
    createElementNS: (_ns, tag) => new FakeElement(tag),
    createTextNode: (text) => new FakeText(text),
    body: new FakeElement("body"),
  };
}
