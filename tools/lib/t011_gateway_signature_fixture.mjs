// Isolated compatibility fixture. Uses a PUBLIC TEST SEED, never deployment keys.
// Usage: node tools/lib/t011_gateway_signature_fixture.mjs /path/to/openclaw/dist
import assert from "node:assert/strict";
import { createHash, createPrivateKey, createPublicKey, sign } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

assert.equal(process.argv.length, 3, "provide the copied 2026.9.8 dist directory");
const sourcePath = resolve(process.argv[2], "device-auth-BT2rF0Jc.mjs");
const { n: buildDeviceAuthPayloadV3 } = await import(pathToFileURL(sourcePath).href);
const seed = Buffer.alloc(32, 7);
const key = createPrivateKey({ key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), seed]), format: "der", type: "pkcs8" });
const rawPublic = createPublicKey(key).export({ format: "der", type: "spki" }).subarray(-32);
const params = {
  deviceId: createHash("sha256").update(rawPublic).digest("hex"),
  clientId: "gateway-client", clientMode: "backend", role: "operator",
  scopes: ["operator.read", "operator.write"], signedAtMs: 1780000000000,
  token: "fixture-token-not-a-credential", nonce: "fixture-nonce",
  platform: " LiNuX ", deviceFamily: " VOICE-Ä ",
};
const payload = buildDeviceAuthPayloadV3(params);
process.stdout.write(JSON.stringify({
  provenance: { version: "2026.9.8", file: "device-auth-BT2rF0Jc.mjs", sha256: createHash("sha256").update(await readFile(sourcePath)).digest("hex") },
  publicTestSeedHex: seed.toString("hex"), params, payload,
  publicKey: rawPublic.toString("base64url"), signature: sign(null, Buffer.from(payload), key).toString("base64url"),
}, null, 2) + "\n");
