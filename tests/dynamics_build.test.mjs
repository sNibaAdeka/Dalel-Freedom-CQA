import assert from 'node:assert/strict';
import test from 'node:test';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const run = promisify(execFile);
const root = fileURLToPath(new URL('../', import.meta.url));
const python = process.env.PYTHON || 'python3';

test('builder emits an isolated static bundle with pre-boot configuration and refuses overwrite', async () => {
  const temporary = await mkdtemp(join(tmpdir(), 'dalel-t50-build-test-'));
  const output = join(temporary, 'site');
  try {
    await run(python, ['dynamics/build_demo.py', '--output', output, '--mode', 'live',
      '--parent-origin', 'https://org.crm4.dynamics.com', '--api-base', 'https://cqa.example.test/api'], { cwd: root });
    const html = await readFile(join(output, 'web/index.html'), 'utf8');
    assert.equal((html.match(/\.applyBootstrap\(/g) || []).length, 1);
    assert.match(html, /import\('\.\.\/dynamics\/bootstrap\.js'\)/);
    assert.match(html, /import\('\.\.\/dynamics\/deployment\.js'\)/);
    assert.doesNotMatch(html, /import\('\.\/dynamics\//);
    assert.ok(html.indexOf('.applyBootstrap(') < html.search(/\.boot\(/));
    const configSource = await readFile(join(output, 'dynamics/deployment.js'), 'utf8');
    const { deployment } = await import(`data:text/javascript;base64,${Buffer.from(configSource).toString('base64')}`);
    assert.equal(deployment.mock, false);
    assert.deepEqual(deployment.parentOrigins, ['https://org.crm4.dynamics.com']);
    assert.deepEqual(deployment.apiBases, ['https://cqa.example.test/api']);
    const paths = await readdir(output, { recursive: true });
    for (const path of paths) {
      assert.match(path, /^(web|dynamics)(\/|$)/);
      assert.doesNotMatch(path, /(?:^|\/)(?:mocks|data|reports|dev|\.git|\.env)(?:\/|$)/);
      assert.doesNotMatch(path, /\.(?:sqlite|jsonl|py|mp3|zip)$/);
    }
    await writeFile(join(output, 'keep.txt'), 'do not overwrite');
    await assert.rejects(run(python, ['dynamics/build_demo.py', '--output', output], { cwd: root }), error => /new directory/.test(error.stderr));
    assert.equal(await readFile(join(output, 'keep.txt'), 'utf8'), 'do not overwrite');
    await assert.rejects(run(python, ['dynamics/build_demo.py', '--output', join(temporary, 'invalid'), '--api-base', 'https://user:secret@example.test/api'], { cwd: root }));
  } finally { await rm(temporary, { recursive: true, force: true }); }
});
