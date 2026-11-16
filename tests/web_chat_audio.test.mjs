import test from "node:test";
import assert from "node:assert/strict";
import { ChatConnection } from "../web_chat/js/net/chat-socket.js";

function connection() {
  const socket = {
    readyState: 1,
    sent: [],
    addEventListener() {},
    send(value) { this.sent.push(JSON.parse(value)); },
  };
  const previous = globalThis.WebSocket;
  globalThis.WebSocket = { OPEN: 1 };
  const result = new ChatConnection(socket);
  return { result, socket, restore: () => { globalThis.WebSocket = previous; } };
}

test("audio send carries the data URL and selected system/agent without text payload", () => {
  const { result, socket, restore } = connection();
  try {
    const audio = `data:audio/wav;base64,${Buffer.from("wav").toString("base64")}`;
    assert.equal(result.sendAudio(audio, { system_key: "sys", agent_key: "agent" }), true);
    assert.deepEqual(socket.sent[0], { message: "", audio, system_key: "sys", agent_key: "agent" });
    assert.equal(socket.sent[0].message, "");
    assert.equal(socket.sent[0].audio, audio);
  } finally { restore(); }
});

test("audio send rejects non-WAV payloads and reports closed socket", () => {
  const { result, socket, restore } = connection();
  try {
    assert.throws(() => result.sendAudio("data:audio/mp3;base64,AA==", {}), /WAV/);
    assert.equal(socket.sent.length, 0);
    socket.readyState = 3;
    assert.equal(result.sendAudio("data:audio/wav;base64,AA==", { system_key: null, agent_key: null }), false);
  } finally { restore(); }
});
