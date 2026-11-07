/** Local demo defaults. build_demo.py emits an environment-specific copy. No secrets here. */
export const deployment = Object.freeze({
  parentOrigins: ['http://127.0.0.1:8000', 'http://localhost:8000', 'http://127.0.0.1:8047', 'http://127.0.0.1:8050'],
  apiBases: ['http://127.0.0.1:8047/api', 'http://127.0.0.1:8000/api', 'http://localhost:8000/api'],
  framePath: '../',
  defaultRole: 'operator',
  defaultConvId: 'demo-ru-001',
  mock: true,
});
