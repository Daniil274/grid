// Pure selection logic: YAML directives are never model records.
export function selectedModelKeys(config, catalog = {}) {
  const section = config.models ?? {};
  const inline = Object.keys(section).filter(key => !["use", "refine"].includes(key));
  const use = section.use;
  const inherited = use === "*" || use === "all" || (use == null && config.models_file && !Object.keys(section).length);
  return [...new Set([
    ...inline,
    ...(inherited ? Object.keys(catalog) : Array.isArray(use) ? use : []),
    ...Object.keys(section.refine ?? {}),
  ])];
}

export function setModelSelected(config, catalog, key, selected) {
  const section = config.models ??= {};
  const use = new Set(selectedModelKeys(config, catalog).filter(name => !(name in section) && !(name in (section.refine ?? {}))));
  selected ? use.add(key) : use.delete(key);
  section.use = [...use];
}
