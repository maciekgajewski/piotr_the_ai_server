// Validates synthetic notification frames against a copied installed Gateway.
// No network, deployment notification, or credentials are used.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

assert.equal(process.argv.length, 3, "provide the copied 2026.9.8 dist directory");
const dist = resolve(process.argv[2]);
const manifest = JSON.parse(await readFile(resolve(dist, "../package.json"), "utf8"));
assert.equal(manifest.version, "2026.9.8");
const builderSource = await readFile(resolve(dist, "node-invoke-request-Eqv0rpYd.mjs"));
assert.equal(createHash("sha256").update(builderSource).digest("hex"), "cab6523d74bc85751bd3939204c0f3e89f48d0c1c38f1769738b3f877cecc0e2");
const protocol = await import(pathToFileURL(resolve(dist, "gateway/protocol/index.js")).href);
const { r: buildNodeInvokeRequest } = await import(pathToFileURL(resolve(dist, "node-invoke-request-Eqv0rpYd.mjs")).href);
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const { invoke, completed, rejected, uncertain } = JSON.parse(Buffer.concat(chunks).toString("utf8"));
assert(protocol.validateNodeInvokeParams(invoke), "node.invoke params rejected");
assert.equal(invoke.command, "system.notify");
const request = buildNodeInvokeRequest({
  id: completed.id,
  nodeId: invoke.nodeId,
  command: invoke.command,
  params: invoke.params,
  timeoutMs: invoke.timeoutMs,
  idempotencyKey: invoke.idempotencyKey,
});
assert.equal(request.id, completed.id);
assert.equal(request.nodeId, completed.nodeId);
assert.equal(request.command, "system.notify");
assert.equal(request.timeoutMs, invoke.timeoutMs);
assert.equal(request.idempotencyKey, invoke.idempotencyKey);
assert.deepEqual(JSON.parse(request.paramsJSON), invoke.params);
assert(protocol.validateNodeInvokeResultParams(completed), "completed node.invoke.result rejected");
assert(protocol.validateNodeInvokeResultParams(rejected), "rejected node.invoke.result rejected");
assert(protocol.validateNodeInvokeResultParams(uncertain), "uncertain node.invoke.result rejected");
assert.equal(completed.ok, true);
assert.equal(rejected.ok, false);
assert.equal(rejected.id, completed.id);
assert.equal(rejected.nodeId, completed.nodeId);
assert.equal(rejected.error.code, "TOO_LONG");
assert.equal(uncertain.error.code, "UNCERTAIN_PLAYBACK");
process.stdout.write(JSON.stringify({ version: manifest.version, checked: 4 }) + "\n");
