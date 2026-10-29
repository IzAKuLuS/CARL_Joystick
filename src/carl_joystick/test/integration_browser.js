"use strict";

// Use the actual controls application client against a live joystick server.
const assert = require("node:assert/strict");
const Client = require(process.argv[3]);
const endpoint = process.argv[2];
const messages = [];
const client = new Client({onState: (message) => messages.push(message)});
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Exercise the real TCP/WebSocket path with deterministic phone-link failures.
// Holding a close handshake reproduces ownership retained by a half-open phone.
const impairment = {replyDelay: 0, driveDelay: 0, holdClose: false};
class ImpairedSocket {
    constructor(url) {
        this.inner = new WebSocket(url);
        this.inner.onopen = () => this.onopen?.();
        this.inner.onclose = (event) => this.onclose?.(event);
        this.inner.onerror = () => this.onerror?.();
        this.inner.onmessage = (event) => {
            const ms = impairment.replyDelay; impairment.replyDelay = 0;
            setTimeout(() => this.onmessage?.(event), ms);
        };
    }
    send(value) {
        const data = JSON.parse(value);
        const ms = data.deadman ? impairment.driveDelay : 0;
        if (ms) {
            impairment.driveDelay = 0;
            setTimeout(() => { if (this.inner.readyState === WebSocket.OPEN) this.inner.send(value); }, ms);
        } else this.inner.send(value);
    }
    close() {
        const ms = impairment.holdClose ? 4000 : 0;
        impairment.holdClose = false;
        setTimeout(() => this.inner.close(), ms);
    }
}

async function impairedRecovery() {
    const states = [];
    const link = new Client({WebSocketClass: ImpairedSocket,
        onState: (message, state) => states.push({message, state})});
    const ready = () => link.connected && link.lastState?.ready
        && !link.lastState.neutral_required && !link.lastState.active;
    const health = () => fetch(endpoint.replace(/^ws/, "http").replace(/\/ws$/, "/health"))
        .then((response) => response.json());
    try {
        link.connect(endpoint);
        await waitFor(ready, "impaired client neutral startup");
        impairment.replyDelay = 350;
        assert.ok(link.begin()); link.move(0, 1);
        await waitFor(() => link.pauseCount === 1, "delayed reply pauses motion");
        await waitFor(ready, "delayed reply neutral recovery");
        assert.equal(link.held, false);
        assert.equal(link.reconnectCount, 0);

        impairment.driveDelay = 300;
        assert.ok(link.begin()); link.move(0, 1);
        await waitFor(() => states.some((s) => s.state?.rejection === "expired_command"), "delayed outbound drive rejection");
        await waitFor(ready, "expired command neutral recovery");
        assert.equal((await health()).active, false);

        assert.ok(link.begin()); link.move(0, 1);
        await waitFor(() => link.lastState?.active, "drive before half-open failure");
        impairment.holdClose = true;
        link.socket.onerror();
        await delay(350);
        assert.equal((await health()).active, false);
        await waitFor(() => states.some((s) => /controller session/.test(s.message)), "old phone still owns session");
        // Server expires the silent owner after 3 s; retries must clear the
        // busy response without a manual Connect click or automatic movement.
        const deadline = performance.now() + 8000;
        while (!ready() && performance.now() < deadline) await delay(20);
        assert.ok(ready(), "half-open session must recover automatically");
        assert.equal(link.held, false);
        assert.ok(link.reconnectCount >= 2);
        assert.equal((await health()).active, false);
        assert.ok(link.begin()); link.move(0, -1);
        await waitFor(() => link.lastState?.active && link.lastState.left === -0.4, "fresh touch after recovery");
        const soak = Number(process.env.CARL_SOAK_SECONDS || 0);
        assert.ok(Number.isFinite(soak) && soak >= 0 && soak <= 300);
        const stableSocket = link.socket;
        const finish = performance.now() + soak * 1000;
        while (performance.now() < finish) {
            await delay(50);
            assert.equal(link.socket, stableSocket, "healthy held-input soak must retain its socket");
            assert.equal(link.lastState?.active, true, "healthy held-input soak must remain active");
            assert.equal(link.lastState?.left, -0.4);
        }
        if (soak) console.log(`Held-input soak passed for ${soak} seconds after impaired-link recovery`);
        link.release(); await waitFor(ready, "release after recovery");
        console.log("Impaired link: delayed replies, expired outbound drive, half-open ownership, busy retries and fresh-touch recovery passed");
    } finally { link.disconnect(); }
}

async function waitFor(predicate, label) {
    const deadline = performance.now() + 3000;
    while (performance.now() < deadline) {
        if (predicate()) return;
        await delay(10);
    }
    throw new Error(`Timed out: ${label}; ${messages.at(-1)}`);
}

async function neutral() {
    await waitFor(() => client.connected && client.lastState?.ready
        && !client.lastState.neutral_required && !client.lastState.active, "neutral readiness");
}

async function drive(x, y, left, right) {
    assert.ok(client.begin(), "fresh touch must be accepted");
    client.move(x, y);
    await waitFor(() => client.lastState?.active && Math.abs(client.lastState.left - left) < 1e-9
        && Math.abs(client.lastState.right - right) < 1e-9, "requested wheel outputs");
    // Allow the independent ROS publishing timer to deliver the command.
    await delay(100);
}

(async () => {
    try {
        client.connect(endpoint);
        await neutral();
        const firstSocket = client.socket;
        await delay(1500); // Idle page remains connected without pointer movement.
        assert.equal(client.socket, firstSocket);
        assert.ok(client.connected);
        for (const [x, y, left, right] of [
            [0, 1, 0.4, 0.4], [0, -1, -0.4, -0.4],
            [-0.05, 1, 0.4, 0.4], [0.05, 1, 0.4, 0.4],
            [-0.05, -1, -0.4, -0.4], [0.05, -1, -0.4, -0.4],
            [0.051, 1, 0.4, 0.3991587802313354],
            [-0.051, 1, 0.3991587802313354, 0.4],
            [-1, 0, -0.4, 0.4], [1, 0, 0.4, -0.4], [1, 1, 0.4, 0]
        ]) {
            await drive(x, y, left, right);
            client.release();
            await neutral();
        }

        const competingMessages = [];
        const competing = new Client({onState: (message) => competingMessages.push(message)});
        try {
            competing.connect(endpoint);
            await waitFor(() => competingMessages.some((m) => /controller session/.test(m)), "exclusive ownership");
            assert.ok(client.connected);
        } finally { competing.disconnect(); }

        client.command("estop");
        await waitFor(() => client.lastState?.estop, "firmware e-stop command");
        assert.equal(client.begin(), false);
        client.command("reset_estop");
        await neutral();
        assert.equal(client.held, false);

        await drive(0, 1, 0.4, 0.4);
        client.socket.close(); // Unexpected connection loss while driving.
        await waitFor(() => !client.connected, "connection loss");
        assert.equal(client.held, false);
        await neutral(); // Transport reconnects automatically, always at rest.
        assert.notEqual(client.socket, firstSocket);
        assert.equal(client.held, false);
        assert.equal(client.lastState.left, 0);
        assert.equal(client.lastState.right, 0);
        console.log("Live controls client: all directions, release, ownership, e-stop/reset, disconnect and neutral reconnect passed");
        client.disconnect();
        await delay(100);
        await impairedRecovery();
    } finally { client.disconnect(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
