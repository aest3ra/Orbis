import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page as ApiPage
from playwright._impl._connection import Connection, ChannelOwner
from playwright._impl._frame import Frame
from playwright._impl._browser_context import BrowserContext
from playwright._impl._page import Page
from playwright._impl._helper import TimeoutSettings
from playwright._impl._transport import Transport
from orbis.crawler import interactor

# The transport supplies protocol replies; no driver or browser process is started.
EVENTS = ('request', 'requestfinished', 'requestfailed')

class ReplyTransport(Transport):
    def __init__(self, loop, mode):
        super().__init__(loop)
        self.mode = mode
        self.connection = None
        self.started = asyncio.Event()
        self.messages = []
        self.evaluate_count = 0
        self.held = None
    async def connect(self): pass
    async def run(self): pass
    def request_stop(self): pass
    async def wait_until_stopped(self): pass
    def send(self, message):
        self.messages.append(message['method'])
        if message['method'] == 'evaluateExpression':
            self.evaluate_count += 1
            self.started.set()
            if self.evaluate_count == 1:
                if self.mode in ('cancel', 'cancel_and_connection_close'):
                    self.held = message['id']
                    return
                if self.mode in ('body_connection_close', 'diagnostic_and_connection_close'):
                    self._loop.call_soon(self.connection.cleanup, 'controlled driver disconnect')
                    return
                if self.mode == 'closed_page':
                    self.connection.dispatch({'guid': 'page', 'method': 'close', 'params': {}})
            if self.mode == 'closed_page':
                self._loop.call_soon(self.on_message, {'id': message['id'], 'error': {'error': {'name': 'TargetClosedError', 'message': 'Target page, context or browser has been closed', 'stack': 'Controlled offline TargetClosedError'}}})
                return
            result = {'value': {'v': 'undefined'}}
        else:
            result = {}
        self._loop.call_soon(self.on_message, {'id': message['id'], 'result': result})

async def exercise(mode):
    loop = asyncio.get_running_loop()
    transport = ReplyTransport(loop, mode)
    connection = Connection(None, lambda *args: None, transport, loop)
    transport.connection = connection
    context = ChannelOwner(connection, 'BrowserContext', 'context', {})
    context._timeout_settings = TimeoutSettings(None)
    context._pages = []
    context._browser = None
    context._close_reason = None
    context._effective_close_reason = MethodType(BrowserContext._effective_close_reason, context)
    frame = Frame(context, 'Frame', 'frame', {'name': '', 'url': 'http://localhost/', 'loadStates': []})
    impl = Page(context, 'Page', 'page', {'mainFrame': frame._channel})
    context._pages.append(impl)
    page = ApiPage(impl)
    if mode == 'closed_connection_before_registration':
        connection.cleanup('closed before subscription')
    pending_cancelled_callbacks = []
    diagnostic_error = OSError('original already latched diagnostic error')
    def check():
        if mode == 'diagnostic_and_connection_close':
            raise diagnostic_error
    task = asyncio.create_task(interactor._perform_and_settle(page, {'kind': 'click'}, check_diagnostics=check))
    if mode in ('cancel', 'cancel_and_connection_close'):
        await asyncio.wait_for(transport.started.wait(), 1)
        task.cancel('original caller cancellation')
        if mode == 'cancel_and_connection_close':
            pending_cancelled_callbacks = [callback.future for callback in connection._callbacks.values() if not callback.no_reply]
            connection.cleanup('controlled driver disconnect after cancellation')
    error = None
    result = None
    try:
        result = await asyncio.wait_for(task, 1)
    except BaseException as exc:
        error = exc
    unconsumed_callback_errors = sum(future.done() and not future.cancelled() and getattr(future, '_log_traceback', False) for future in pending_cancelled_callbacks)
    for future in pending_cancelled_callbacks:
        if future.done() and not future.cancelled():
            future.exception()
    pending_before_reply = len(connection._callbacks)
    if transport.held in connection._callbacks:
        connection.dispatch({'id': transport.held, 'result': {'value': {'v': 'undefined'}}})
    counts = {event: len(impl.listeners(event)) for event in EVENTS}
    if mode.startswith('cancel'):
        expected = isinstance(error, asyncio.CancelledError) and error.args == ('original caller cancellation',) and task.cancelled()
    elif mode == 'closed_connection_before_registration':
        expected = type(error).__name__ == 'TargetClosedError' and transport.messages == []
    elif mode == 'diagnostic_and_connection_close':
        expected = error is diagnostic_error
    elif mode == 'body_connection_close':
        expected = type(error).__name__ == 'TargetClosedError'
    else:
        expected = error is None and result is False
    passed = expected and not any(counts.values()) and not connection._callbacks
    output = {'mode': mode, 'passed': passed, 'result': result, 'error': type(error).__name__ if error else None, 'error_args': list(error.args) if error else [], 'cancelled': task.cancelled(), 'original_diagnostic_identity': error is diagnostic_error, 'remaining_listeners': counts, 'unconsumed_cancelled_protocol_future_errors_before_audit_cleanup': unconsumed_callback_errors, 'transport_methods': transport.messages, 'evaluate_count': transport.evaluate_count, 'callbacks_pending_before_delayed_peer_reply': pending_before_reply, 'callbacks_pending_after_reply': len(connection._callbacks)}
    connection.cleanup('audit finished')
    await asyncio.sleep(0)
    assert not [other for other in asyncio.all_tasks() if other is not asyncio.current_task()]
    return output


@pytest.mark.parametrize("mode", [
    "normal", "closed_page", "closed_connection_before_registration", "body_connection_close",
    "cancel", "cancel_and_connection_close", "diagnostic_and_connection_close",
])
def test_actual_page_subscription_cleanup_preserves_primary(mode):
    result = asyncio.run(exercise(mode))
    assert result["passed"], result


@pytest.fixture
def cleanup_case(monkeypatch):
    state = SimpleNamespace(primary=None, cancel=False, cleanup={}, observer_error=None,
                            diagnostic=None, latch_at=None, latched=False, removed=[], observer_calls=0,
                            error=None, result=None, cancelled=False, listeners={})

    def check():
        if state.latched:
            raise state.diagnostic

    def remove(event, callback):
        assert state.listeners.pop(event) is callback
        state.removed.append(event)
        if state.latch_at == event:
            state.latched = True
        if event in state.cleanup:
            raise state.cleanup[event]

    async def run():
        entered = asyncio.Event()

        async def evaluate(script):
            if script == interactor._WATCH_DOM_JS:
                if state.cancel:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError as exc:
                        state.primary = exc
                        raise
                if state.primary is not None:
                    raise state.primary
            else:
                state.observer_calls += 1
                if state.latch_at == 'observer':
                    state.latched = True
                if state.observer_error is not None:
                    raise state.observer_error

        page = SimpleNamespace(evaluate=evaluate, remove_listener=remove,
                               on=lambda event, callback: state.listeners.__setitem__(event, callback))
        task = asyncio.create_task(interactor._perform_and_settle(page, {}, check_diagnostics=check))
        if state.cancel:
            await entered.wait()
            task.cancel('original caller cancellation')
        try:
            state.result = await task
        except BaseException as exc:
            state.error = exc
        state.cancelled = task.cancelled()
        assert not [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]

    monkeypatch.setattr(interactor, '_perform', AsyncMock(return_value=False))
    state.run = lambda: asyncio.run(run())
    return state


@pytest.mark.parametrize('mode', ['normal', 'runtime', 'cancel'])
@pytest.mark.parametrize('event', EVENTS)
@pytest.mark.parametrize('observer_failure', [False, True])
def test_listener_failure_attempts_remaining_cleanup_and_preserves_primary(cleanup_case, mode, event, observer_failure):
    state = cleanup_case
    first = OSError('private listener failure')
    state.cleanup[event] = first
    if mode == 'runtime':
        state.primary = RuntimeError('original body failure')
        state.primary.__cause__ = ValueError('existing cause')
        state.primary.__context__ = LookupError('existing context')
    state.cancel = mode == 'cancel'
    if observer_failure:
        state.observer_error = OSError('private observer failure')
    cause = getattr(state.primary, '__cause__', None)
    context = getattr(state.primary, '__context__', None)
    state.run()
    assert state.error is (first if mode == 'normal' else state.primary)
    assert state.removed == list(EVENTS)
    assert state.observer_calls == 1 and state.listeners == {}
    assert state.cancelled == (mode == 'cancel')
    if mode == 'cancel':
        assert state.error.args == ('original caller cancellation',)
    if mode == 'runtime':
        assert state.error.__cause__ is cause
        assert state.error.__context__ is context
    notes = [] if mode == 'normal' else [f'Listener removal failed ({event}): OSError']
    if observer_failure:
        notes.append('Observer disconnect cleanup failed: OSError')
    assert getattr(state.error, '__notes__', []) == notes


def test_first_listener_error_preserves_later_cleanup_types(cleanup_case):
    state = cleanup_case
    failures = [OSError('first'), RuntimeError('second'), ValueError('third')]
    state.cleanup = dict(zip(EVENTS, failures))
    state.observer_error = OSError('fourth')
    state.run()
    assert state.error is failures[0]
    assert state.error.__notes__ == [
        'Listener removal failed (requestfinished): RuntimeError',
        'Listener removal failed (requestfailed): ValueError',
        'Observer disconnect cleanup failed: OSError',
    ]
    assert state.removed == list(EVENTS) and state.observer_calls == 1


@pytest.mark.parametrize('mode', ['normal', 'runtime', 'cancel'])
@pytest.mark.parametrize('event', EVENTS)
def test_new_listener_interruption_stops_loop_but_attempts_observer(cleanup_case, mode, event):
    state = cleanup_case
    interruption = BaseException('new cleanup interruption')
    state.cleanup[event] = interruption
    state.observer_error = OSError('secondary observer failure')
    state.primary = RuntimeError('old body failure') if mode == 'runtime' else None
    state.cancel = mode == 'cancel'
    state.run()
    assert state.error is interruption
    assert state.error.__notes__ == ['Observer disconnect cleanup failed: OSError']
    assert state.removed == list(EVENTS[:EVENTS.index(event) + 1])
    assert state.observer_calls == 1


@pytest.mark.parametrize('first_type', [OSError, BaseException])
def test_new_observer_interruption_precedes_listener_error(cleanup_case, first_type):
    state = cleanup_case
    state.cleanup['request'] = first_type('earlier listener failure')
    state.observer_error = BaseException('new observer interruption')
    state.run()
    assert state.error is state.observer_error
    assert state.observer_calls == 1


@pytest.mark.parametrize('event', ['request', 'observer'])
@pytest.mark.parametrize('diagnostic_type', [OSError, PlaywrightError, BaseException])
def test_late_diagnostic_precedes_ordinary_cleanup_error(cleanup_case, event, diagnostic_type):
    state = cleanup_case
    state.diagnostic = diagnostic_type('original diagnostic failure')
    state.latch_at = event
    if event == 'observer':
        state.observer_error = OSError('later observer failure')
    else:
        state.cleanup[event] = OSError('later listener failure')
    state.run()
    assert state.error is state.diagnostic
    label = 'Observer disconnect cleanup failed' if event == 'observer' else f'Listener removal failed ({event})'
    assert state.error.__notes__ == [f'{label}: OSError']
    assert state.observer_calls == 1
