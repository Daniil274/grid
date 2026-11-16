import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { selectedModelKeys, setModelSelected } from '../web_chat/js/settings/model-selection.js';
const drawer = await readFile(new URL('../web_chat/js/settings/drawer.js', import.meta.url), 'utf8');

test('directives are not model records; selected and refined entries are offered', () => {
  assert.deepEqual(selectedModelKeys({models: {use: ['m'], refine: {r: {temperature: 0.2}}}}), ['m', 'r']);
});
test('wildcard selection can be narrowed', () => {
  const config = {models: {use: '*'}};
  setModelSelected(config, {a: {}, b: {}, c: {}}, 'b', false);
  assert.deepEqual(config.models.use, ['a', 'c']);
});
test('selection preserves legacy records and refinements', () => {
  const config = {models: {legacy: {name: 'old', provider: 'p'}, refine: {a: {temperature: 0.2}}}};
  const before = structuredClone(config);
  setModelSelected(config, {a: {}, b: {}}, 'b', true);
  assert.deepEqual(config.models.legacy, before.models.legacy);
  assert.deepEqual(config.models.refine, before.models.refine);
  assert.deepEqual(selectedModelKeys(config), ['legacy', 'b', 'a']);
});
test('only global targets expose definition editors', () => {
  assert.match(drawer, /\["models", "providers"\]\.includes\(this\.payload\.kind\)/);
  const systemTabs = drawer.slice(drawer.indexOf('    return [\n      ["general"'), drawer.indexOf('\n  _renderTabs()'));
  assert.doesNotMatch(systemTabs, /_collectionPane\("models"|_collectionPane\("providers"/);
  assert.match(systemTabs, /_modelSelectionPane/);
});
test('both saves propagate revision', () => {
  assert.match(drawer, /api\.saveYaml\(this\.yamlArea\.value, this\.target, this\.payload\.revision\)/);
  assert.match(drawer, /api\.saveSettings\(this\.config, this\.target, this\.payload\.revision\)/);
});
