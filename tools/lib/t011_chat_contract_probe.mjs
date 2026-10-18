// Validates synthetic chat frames against a copied installed Gateway. No network.
// Never feed deployment transcripts or credentials to this fixture.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

assert.equal(process.argv.length, 3, "provide the copied 2026.9.8 dist directory");
const dist = resolve(process.argv[2]);
const manifest = JSON.parse(await readFile(resolve(dist, "../package.json"), "utf8"));
assert.equal(manifest.version, "2026.9.8");
const schemaSource = await readFile(resolve(dist, "sessions-Bw59kgfQ.mjs"));
assert.equal(createHash("sha256").update(schemaSource).digest("hex"), "044fb9b42bd88b3d6b326b527ff61dc029c47517fcdbd7e75629a98ec4730734");
const protocol = await import(pathToFileURL(resolve(dist, "gateway/protocol/index.js")).href);
const requireFromPackage = createRequire(resolve(dist, "../package.json"));
const Ajv = requireFromPackage("ajv");
const validateEvent = new Ajv({ strict: false }).compile(protocol.ChatEventSchema);
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const { send, abort, wait, terminal } = JSON.parse(Buffer.concat(chunks).toString("utf8"));
assert(protocol.validateChatSendParams(send), "chat.send params rejected");
assert(protocol.validateChatAbortParams(abort), "chat.abort params rejected");
assert(protocol.validateAgentWaitParams(wait), "agent.wait params rejected");
assert(validateEvent(terminal), "chat terminal event rejected");
assert.equal(send.idempotencyKey, abort.runId);
assert.equal(send.idempotencyKey, terminal.runId);
assert.equal(send.idempotencyKey, wait.runId);
assert.equal(send.sessionKey, abort.sessionKey);
assert.equal(send.sessionKey, terminal.sessionKey);
process.stdout.write(JSON.stringify({ version: manifest.version, checked: 4 }) + "\n");
