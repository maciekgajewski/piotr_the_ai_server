// Validates TEST connect frames against a copied installed Gateway. No network.
// Never feed deployment credentials to this fixture.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

assert.equal(process.argv.length, 3, "provide the copied 2026.9.8 dist directory");
const dist = resolve(process.argv[2]);
const manifest = JSON.parse(await readFile(resolve(dist, "../package.json"), "utf8"));
assert.equal(manifest.version, "2026.9.8");
const sourceName = "device-auth-BT2rF0Jc.mjs";
const fixture = JSON.parse(await readFile(new URL("../../tests/fixtures/openclaw-2026.9.8-device-auth.json", import.meta.url), "utf8"));
assert.equal(createHash("sha256").update(await readFile(resolve(dist, sourceName))).digest("hex"), fixture.provenance.sha256);
const { n: buildDeviceAuthPayloadV3 } = await import(pathToFileURL(resolve(dist, sourceName)).href);
const { d: verifySignature } = await import(pathToFileURL(resolve(dist, "ed25519-signature-D_xiObSW.mjs")).href);
const protocol = await import(pathToFileURL(resolve(dist, "gateway/protocol/index.js")).href);
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const frames = JSON.parse(Buffer.concat(chunks).toString("utf8"));
assert.equal(frames.length, 2);
assert.equal(protocol.PROTOCOL_VERSION, 4);
for (const frame of frames) {
  assert(protocol.validateRequestFrame(frame), "request envelope rejected");
  assert(protocol.validateConnectParams(frame.params), "connect params rejected");
  const { device, client, auth, role, scopes } = frame.params;
  const payload = buildDeviceAuthPayloadV3({
    deviceId: device.id, clientId: client.id, clientMode: client.mode, role, scopes,
    signedAtMs: device.signedAt, token: auth.token ?? auth.deviceToken,
    nonce: device.nonce, platform: client.platform, deviceFamily: client.deviceFamily,
  });
  assert(verifySignature({ publicKey: device.publicKey, payload, signatureBase64Url: device.signature }), "signature rejected");
  assert.equal(createHash("sha256").update(Buffer.from(device.publicKey, "base64url")).digest("hex"), device.id);
}
assert.notEqual(frames[0].params.device.id, frames[1].params.device.id);
process.stdout.write(JSON.stringify({ version: manifest.version, protocol: protocol.PROTOCOL_VERSION, checked: frames.length }) + "\n");
