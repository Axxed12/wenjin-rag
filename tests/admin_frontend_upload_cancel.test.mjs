import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const html = readFileSync(resolve(root, 'src/wenjin/file_management/admin/index.html'), 'utf8');

function elementAttributes(markup, tagName) {
  return [...markup.matchAll(new RegExp(`<${tagName}\\b([^>]*)>`, 'gi'))]
    .map((match) => Object.fromEntries([...match[1].matchAll(/([\w-]+)(?:="([^"]*)")?/g)]
      .map((attribute) => [attribute[1].toLowerCase(), attribute[2] ?? true])));
}

test('upload dialog controls use click actions instead of required-field form submission', () => {
  const dialog = html.match(/<dialog\b(?=[^>]*\bid="upload-dialog")[^>]*>([\s\S]*?)<\/dialog>/i)?.[1];
  assert.ok(dialog, 'the upload dialog exists');
  assert.match(dialog, /<select\b(?=[^>]*\bid="upload-library")/i, 'the destination selector remains available');
  assert.doesNotMatch(dialog, /<select\b(?=[^>]*\bid="upload-library")(?=[^>]*\brequired\b)/i,
    'choosing a library is checked by the explicit upload action, not native submit validation');
  assert.doesNotMatch(dialog, /<form\b/i, 'cancel and close do not submit an upload form');

  const buttons = elementAttributes(dialog, 'button');
  for (const id of ['upload-close-button', 'upload-cancel-button', 'choose-upload-file']) {
    const button = buttons.find((item) => item.id === id);
    assert.ok(button, `${id} is present`);
    assert.equal(button.type, 'button', `${id} cannot accidentally submit a form`);
  }
});
