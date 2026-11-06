"use strict";

// Use the actual controls application client against a live joystick server.
const assert = require("node:assert/strict");
const Client = require(process.argv[3]);
const path = require("node:path");
const endpoint = process.argv[2];
const messages = [];
const Diagnostics = require(path.join(path.dirname(process.argv[3]), "joystick-diagnostics.js"));
const diagnostics = new Diagnostics({indexedDB: null});
diagnostics.setEndpoint(endpoint);
const client = new Client({onState: (message) => messages.push(message),
    onDiagnostic: (event, data) => diagnostics.record(event, data)});
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
            // Preserve native message-before-close ordering unless delay is
            // explicitly injected. A zero-delay timer can otherwise deliver a
            // busy reply after close and erase the evidence under test.
            if (ms) setTimeout(() => this.onmessage?.(event), ms);
            else this.onmessage?.(event);
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
        onDiagnostic: (event, data) => diagnostics.record(event, data),
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
        await waitFor(() => states.some((s) => /controller session/.test(s.message)), "old phone still owns session")
            .catch(error => { error.message += `; recovery states=${JSON.stringify(states.slice(-12).map(s => s.message))}`; throw error; });
        // Server expires the silent owner after 3 s; retries must clear the
        // busy response without a manual Connect click or automatic movement.
        const deadline = performance.now() + 8000;
        while (!ready() && performance.now() < deadline) await delay(20);
        assert.ok(ready(), "half-open session must recover automatically");
        assert.equal(link.held, false);
        assert.ok(link.reconnectCount >= 2);
        assert.equal((await health()).active, false);
        assert.ok(link.begin()); link.move(0, -1);
        await waitFor(() => link.lastState?.active && link.lastState.left === -0.75, "fresh touch after recovery");
        const soak = Number(process.env.CARL_SOAK_SECONDS || 0);
        assert.ok(Number.isFinite(soak) && soak >= 0 && soak <= 300);
        const stableSocket = link.socket;
        const finish = performance.now() + soak * 1000;
        while (performance.now() < finish) {
            await delay(50);
            assert.equal(link.socket, stableSocket, "healthy held-input soak must retain its socket");
            assert.equal(link.lastState?.active, true, "healthy held-input soak must remain active");
            assert.equal(link.lastState?.left, -0.75);
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

async function throttleCurveUI() {
    // Actual native input + UI + live WebSocket transport, sharing the DOM
    // harness with the pointer interruption regressions in the controls repo.
    const {setup} = require(path.join(path.dirname(process.argv[3]), "../tests/helpers/joystick-dom.js"));
    const ui = setup({live: true, endpoint});
    const link = ui.client;
    const element = ui.element;
    const ready = () => link.connected && link.lastState?.ready
        && !link.lastState.neutral_required && !link.lastState.active;
    try {
        ui.connect();
        await waitFor(ready, "UI neutral startup");
        assert.equal(element("joystick-limit").textContent, "75.0%");
        assert.equal(link.lastState.throttle_exponent, 2);
        for (const sign of [-1, 1]) {
            for (const [travel, output] of [[0, 0], [0.025, 0], [0.05, 0],
                [0.051, 0.0000008310249307479125], [0.1, 0.002077562326869806],
                [0.2, 0.018698060941828257], [0.205, 0.019965373961218832],
                [0.25, 0.0332409972299169], [0.5, 0.1682825484764543],
                [0.75, 0.40720221606648194], [0.95, 0.6731301939058171], [1, 0.75]]) {
                ui.pointer("pointerdown");
                ui.move(0, sign * travel);
                const expected = sign * output;
                await waitFor(() => link.lastState?.active
                    && Math.abs(link.lastState.left - expected) < 1e-9
                    && Math.abs(link.lastState.right - expected) < 1e-9, "quadratic UI wheel command");
                assert.equal(element("joystick-left").textContent, `${(expected * 100).toFixed(1)}%`);
                assert.equal(element("joystick-right").textContent, `${(expected * 100).toFixed(1)}%`);
                await delay(100); // Let ROS and the real bridge encode the sample.
                ui.pointer("pointerup");
                assert.equal(element("joystick-left").textContent, "0.0%");
                await waitFor(ready, "UI release");
            }
        }
        for (const type of ["contextmenu", "dragstart", "pointercancel", "lostpointercapture", "touchcancel"]) {
            ui.pointer("pointerdown");
            await waitFor(() => link.lastState?.active && link.lastState.left === 0.75, "drive before native interruption");
            await delay(300); // Stationary touch must remain a valid hold.
            assert.equal(link.held, true);
            if (type === "pointercancel" || type === "lostpointercapture") ui.document.fire(type, {pointerId: 1});
            else if (type === "touchcancel") ui.document.fire(type, {touches: []});
            else ui.zone.fire(type);
            assert.equal(link.held, false);
            assert.equal(ui.captured.size, 0);
            await waitFor(ready, "native interruption commands zero");
            assert.equal(link.lastState.left, 0); assert.equal(link.lastState.right, 0);
            ui.move(0, 1); await delay(100);
            assert.equal(link.lastState.active, false, "old gesture cannot re-arm movement");
        }
        ui.pointer("pointerdown");
        await waitFor(() => link.lastState?.active, "fresh touch recovers without reload");
        ui.element("joystick-disconnect").fire("click");
        assert.equal(ui.captured.size, 0);
        await delay(100); ui.connect(); await waitFor(ready, "UI reconnect at neutral");
        ui.move(0, 1); assert.equal(link.held, false);
        ui.pointer("pointerdown");
        await waitFor(() => link.lastState?.active, "fresh touch works after reconnect");
        ui.pointer("pointerup"); await waitFor(ready, "final UI release");
        console.log("Actual UI + live client: quadratic throttle, center deadzone, 75% cap, signed percentages and release passed");
        console.log("Native interruptions: copy menu, drag, cancellation, lost capture, stale moves, fresh-touch recovery and reconnect passed");
    } finally { link?.disconnect(); }
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
            [0, 1, 0.75, 0.75], [0, -1, -0.75, -0.75],
            [-0.05, 1, 0.75, 0.75], [0.05, 1, 0.75, 0.75],
            [-0.05, -1, -0.75, -0.75], [0.05, -1, -0.75, -0.75],
            [0.051, 1, 0.75, 0.7491583672324308],
            [-0.051, 1, 0.7491583672324308, 0.75],
            [0.5, 0.5, 0.35775623268698065, -0.021191135734072053],
            [-0.5, 0.5, -0.021191135734072053, 0.35775623268698065],
            [0.5, -0.5, 0.021191135734072053, -0.35775623268698065],
            [0.6, 0.8, 0.6990304709141274, 0.23587257617728538],
            [-1, 0, -0.4, 0.4], [1, 0, 0.4, -0.4], [1, 1, 0.75, 0.2282608695652174]
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

        await drive(0, 1, 0.75, 0.75);
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
        await throttleCurveUI();
        await delay(100);
        await impairedRecovery();
    } finally { client.disconnect(); await diagnostics.flush(); diagnostics.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
