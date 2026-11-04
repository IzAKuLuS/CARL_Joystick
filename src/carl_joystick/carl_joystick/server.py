"""WebSocket ownership and input adapter, independent of ROS imports."""
from contextlib import asynccontextmanager
import asyncio
import json
import logging
import re
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from carl_joystick.session import CommandLease


# Connection ownership outlives the motion deadline, but never indefinitely.
# Healthy idle browsers still send neutral keepalives at 20 Hz.
SESSION_TIMEOUT = 3.0


def create_app(node, controls_dir=''):
    logger = node.get_logger() if hasattr(node, 'get_logger') else logging.getLogger(__name__)
    recorder = getattr(node, 'recorder', None)

    def record(event, **fields):
        if recorder:
            recorder.emit(event, **fields)

    async def loop_monitor():
        previous = time.monotonic()
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            record('event_loop', interval_s=now - previous, lag_s=max(0, now - previous - 1))
            previous = now

    @asynccontextmanager
    async def lifespan(app):
        monitor = asyncio.create_task(loop_monitor())
        try:
            yield
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
            with node.lock:
                node.controller.stop('server_shutdown')

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['GET', 'POST'],
                       allow_headers=['Content-Type'])

    @app.websocket('/ws')
    async def websocket_endpoint(websocket: WebSocket):
        owner = object()
        session_id = uuid.uuid4().hex
        peer = websocket.client.host if websocket.client else None
        record('session_attempt', session_id=session_id, peer=peer,
               server_address=websocket.scope.get('server'),
               user_agent=websocket.headers.get('user-agent', '')[:256])
        await websocket.accept()
        with node.lock:
            if node.controller.owner is None:
                node.controller.session_id = session_id
                node.controller.packet_seq = None
            claimed = node.controller.claim(owner)
        if not claimed:
            record('session_rejected', session_id=session_id, peer=peer, reason='controller_busy')
            await websocket.send_json({'error': 'Another controller is connected',
                                       'code': 'controller_busy'})
            await websocket.close(code=1008)
            return
        record('session_connected', session_id=session_id, peer=peer)
        logger.info('Joystick session connected')
        lease = CommandLease(node.controller.input_timeout, node.controller.now)
        close_reason = 'disconnected'
        rejected = False
        packet_seq = 0
        phase = 'receive'
        last_receive = time.monotonic()
        last_send = None
        try:
            # Every packet receives an acknowledgement. The browser allows only
            # one outstanding packet, bounding queued control input on slow links.
            while True:
                # Keep idle sessions; the ROS steady timer expires motion.
                phase = 'receive'
                raw = await asyncio.wait_for(websocket.receive_text(), SESSION_TIMEOUT)
                received = time.monotonic()
                packet_seq += 1
                record('packet_received', session_id=session_id, packet_seq=packet_seq,
                       bytes=len(raw.encode()), interval_s=received - last_receive)
                last_receive = received
                if len(raw) > 1024:
                    raise ValueError('Joystick packet too large')
                data = json.loads(raw)
                record('command_received', session_id=session_id, packet_seq=packet_seq,
                       command={k: data[k] for k in ('type', 'x', 'y', 'deadman', 'client_seq',
                                'client_id', 'client_mono_ms', 'client_wall_ms') if k in data}
                       if isinstance(data, dict) else {'invalid_type': type(data).__name__})
                phase = 'handle'
                with node.lock:
                    node.controller.packet_seq = packet_seq
                    # Neutral and e-stop always work, including on an expired
                    # lease. Drive and reset must echo the latest server token.
                    requires_lease = (isinstance(data, dict)
                                      and (data.get('type') == 'reset_estop'
                                           or (data.get('type') == 'control'
                                               and data.get('deadman') is True)))
                    if requires_lease and not lease.consume(data.get('token')):
                        node.controller.stop('expired_command')
                        state = node.controller.state()
                        state['rejection'] = 'expired_command'
                        if not rejected:
                            logger.warning('Joystick rejected delayed or unrecognized command; neutral required')
                        rejected = True
                    else:
                        state = node.controller.handle(owner, data)
                        rejected = False
                    record('command_result', session_id=session_id, packet_seq=packet_seq,
                           client_seq=data.get('client_seq') if isinstance(data, dict) else None,
                           state=state, processing_ms=(time.monotonic() - received) * 1000)
                    state.update(protocol=2, token=lease.issue())
                    if recorder:
                        state.update(diagnostics=1, run_id=recorder.run_id, session_id=session_id,
                                     packet_seq=packet_seq, client_seq=data.get('client_seq'),
                                     server_mono_ns=str(time.monotonic_ns()))
                phase = 'send'
                send_started = time.monotonic()
                await asyncio.wait_for(websocket.send_json(state), SESSION_TIMEOUT)
                last_send = time.monotonic()
                record('reply_sent', session_id=session_id, packet_seq=packet_seq,
                       send_ms=(last_send - send_started) * 1000,
                       total_ms=(last_send - received) * 1000)
        except WebSocketDisconnect as error:
            close_reason = f'disconnected code={error.code}'
            record('websocket_disconnect', session_id=session_id, phase=phase, code=error.code,
                   reason=getattr(error, 'reason', ''))
        except asyncio.TimeoutError:
            close_reason = 'session_timeout'
            record('session_timeout', session_id=session_id, phase=phase)
            # Free ownership before waiting for any network close handshake.
            with node.lock:
                node.controller.stop('session_timeout_' + phase)
                node.controller.release(owner)
            await websocket.close(code=1001, reason='Joystick session timed out')
        except (ValueError, TypeError) as error:
            record('packet_error', session_id=session_id, packet_seq=packet_seq,
                   error_type=type(error).__name__, detail=str(error))
            close_reason = 'invalid_packet'
            with node.lock:
                node.controller.stop('invalid_packet')
            await websocket.close(code=1008, reason='Invalid joystick packet')
        except Exception as error:
            close_reason = 'server_error'
            record('server_error', session_id=session_id, phase=phase,
                   error_type=type(error).__name__, detail=str(error), traceback=traceback.format_exc())
            raise
        finally:
            with node.lock:
                node.controller.release(owner)
                if node.controller.owner is None:
                    node.controller.session_id = None
                    node.controller.packet_seq = None
            record('session_closed', session_id=session_id, reason=close_reason, phase=phase,
                   packets=packet_seq, receive_age_s=time.monotonic() - last_receive,
                   send_age_s=None if last_send is None else time.monotonic() - last_send)
            logger.info('Joystick session closed: ' + close_reason)

    @app.get('/health')
    async def health():
        with node.lock:
            state = node.controller.state()
        if recorder:
            state['logging'] = recorder.status()
        return state

    @app.post('/diagnostics')
    async def diagnostics(request: Request):
        # Independent of command ownership/leases. Bounded even for chunked uploads.
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 65536:
                return JSONResponse({'error': 'diagnostic batch too large'}, status_code=413)
        try:
            batch = json.loads(payload)
            if not isinstance(batch, list) or not 1 <= len(batch) <= 100:
                raise ValueError('Expected 1–100 events')
            if any(not isinstance(item, dict) or not isinstance(item.get('event'), str)
                   or not re.fullmatch(r'[a-z0-9_]{1,80}', item['event'])
                   or ('id' in item and (not isinstance(item['id'], str) or len(item['id']) > 200))
                   or ('data' in item and not isinstance(item['data'], dict)) for item in batch):
                raise ValueError('Expected bounded diagnostic event objects')
        except (ValueError, TypeError):
            return JSONResponse({'error': 'invalid diagnostic batch'}, status_code=400)
        if not recorder:
            return JSONResponse({'error': 'recording unavailable'}, status_code=503)
        status = recorder.status()
        if status['error'] or not status['writer_alive'] or recorder.queue.full():
            return JSONResponse({'error': 'recording unhealthy; retain browser copy'}, status_code=503)
        for item in batch:
            record('browser_event', peer=request.client.host if request.client else None, browser=item)
        # Acknowledges enqueue, not a crash-proof disk commit. Browser keeps its local copy.
        return JSONResponse({'accepted': len(batch), 'run_id': recorder.run_id},
                            headers={'Access-Control-Allow-Origin': '*'})

    if controls_dir:
        directory = Path(controls_dir).expanduser().resolve()
        if not (directory / 'joystick.html').is_file():
            raise ValueError('controls_dir must contain the CARL controls HTML files')
        app.mount('/', StaticFiles(directory=directory, html=True), name='controls')
    return app

