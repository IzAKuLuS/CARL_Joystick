"""WebSocket ownership and input adapter, independent of ROS imports."""
from contextlib import asynccontextmanager
import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from carl_joystick.session import CommandLease


# Connection ownership outlives the motion deadline, but never indefinitely.
# Healthy idle browsers still send neutral keepalives at 20 Hz.
SESSION_TIMEOUT = 3.0


def create_app(node, controls_dir=''):
    logger = node.get_logger() if hasattr(node, 'get_logger') else logging.getLogger(__name__)
    @asynccontextmanager
    async def lifespan(app):
        yield
        with node.lock:
            node.controller.stop()

    app = FastAPI(lifespan=lifespan)

    @app.websocket('/ws')
    async def websocket_endpoint(websocket: WebSocket):
        owner = object()
        await websocket.accept()
        with node.lock:
            claimed = node.controller.claim(owner)
        if not claimed:
            await websocket.send_json({'error': 'Another controller is connected',
                                       'code': 'controller_busy'})
            await websocket.close(code=1008)
            return
        logger.info('Joystick session connected')
        lease = CommandLease(node.controller.input_timeout, node.controller.now)
        close_reason = 'disconnected'
        rejected = False
        try:
            # Every packet receives an acknowledgement. The browser allows only
            # one outstanding packet, bounding queued control input on slow links.
            while True:
                # Keep idle sessions; the ROS steady timer expires motion.
                raw = await asyncio.wait_for(websocket.receive_text(), SESSION_TIMEOUT)
                if len(raw) > 1024:
                    raise ValueError('Joystick packet too large')
                data = json.loads(raw)
                with node.lock:
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
                    state.update(protocol=2, token=lease.issue())
                await asyncio.wait_for(websocket.send_json(state), SESSION_TIMEOUT)
        except WebSocketDisconnect as error:
            close_reason = f'disconnected code={error.code}'
        except asyncio.TimeoutError:
            close_reason = 'session_timeout'
            # Free ownership before waiting for any network close handshake.
            with node.lock:
                node.controller.release(owner)
            await websocket.close(code=1001, reason='Joystick session timed out')
        except (ValueError, TypeError):
            close_reason = 'invalid_packet'
            await websocket.close(code=1008, reason='Invalid joystick packet')
        finally:
            with node.lock:
                node.controller.release(owner)
            logger.info('Joystick session closed: ' + close_reason)

    @app.get('/health')
    async def health():
        with node.lock:
            return node.controller.state()

    if controls_dir:
        directory = Path(controls_dir).expanduser().resolve()
        if not (directory / 'joystick.html').is_file():
            raise ValueError('controls_dir must contain the CARL controls HTML files')
        app.mount('/', StaticFiles(directory=directory, html=True), name='controls')
    return app

