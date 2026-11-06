/**
 * Unified logging — src/logUi.js, the renderer log helper.
 *
 * Pins the three disciplines the feature promises: message types
 * are traced for the allowlist only (payloads never cross),
 * window errors are forwarded deduped, and the whole helper is
 * inert when the send bridge is absent or broken.
 */
import {
  createUiLogger,
  installUiLogging,
  isAllowlistedMessageType,
  messageTypeOf,
  MESSAGE_TYPE_ALLOWLIST_PREFIXES,
} from '../logUi';

describe('isAllowlistedMessageType', () => {
  it('admits exactly the spec prefixes', () => {
    expect(MESSAGE_TYPE_ALLOWLIST_PREFIXES).toEqual([
      'MODEL_', 'TASK_', 'PROVIDER_', 'DIAGNOSTICS_', 'CONFIG_', 'ENVIRONMENT_',
    ]);
    for (const type of [
      'MODEL_SET_DEFAULT', 'MODEL_DEFAULT_CHANGED',
      'TASK_PROGRESS', 'PROVIDER_LIST_REQUEST',
      'DIAGNOSTICS_EXPORT_REQUEST', 'CONFIG_GET_RESPONSE',
      'ENVIRONMENT_DIAGNOSTIC_RESPONSE',
    ]) {
      expect(isAllowlistedMessageType(type)).toBe(true);
    }
  });

  it('drops chat/session/traffic types and junk', () => {
    for (const type of ['CHAT_MESSAGE', 'SESSION_LIST_REQUEST', 'START_TASK', '_ws_status', '', null, undefined, 42]) {
      expect(isAllowlistedMessageType(type)).toBe(false);
    }
  });
});

describe('messageTypeOf', () => {
  it('reads the type out of a serialized envelope', () => {
    expect(messageTypeOf(JSON.stringify({ type: 'MODEL_SET_DEFAULT', payload: {} }))).toBe('MODEL_SET_DEFAULT');
  });

  it('returns null for non-JSON, typeless and non-string input', () => {
    expect(messageTypeOf('not json')).toBe(null);
    expect(messageTypeOf(JSON.stringify({ payload: {} }))).toBe(null);
    expect(messageTypeOf({ type: 'MODEL_SET_DEFAULT' })).toBe(null);
    expect(messageTypeOf(null)).toBe(null);
  });
});

describe('createUiLogger', () => {
  it('forwards allowlisted message types as DEBUG entries and drops the rest', () => {
    const send = jest.fn();
    const logger = createUiLogger(send);
    logger.logMessageType('out', 'MODEL_SET_DEFAULT');
    logger.logMessageType('in', 'TASK_PROGRESS');
    logger.logMessageType('out', 'CHAT_MESSAGE');
    logger.logMessageType('sideways', 'MODEL_SET_DEFAULT');
    expect(send).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenNthCalledWith(1, { source: 'ui', level: 'DEBUG', message: 'ws message out: MODEL_SET_DEFAULT' });
    expect(send).toHaveBeenNthCalledWith(2, { source: 'ui', level: 'DEBUG', message: 'ws message in: TASK_PROGRESS' });
  });

  it('maps the two real socket events to levels and includes the detail', () => {
    const send = jest.fn();
    const logger = createUiLogger(send);
    logger.logWsEvent('open');
    logger.logWsEvent('close', 'code 1006');
    logger.logWsEvent('mystery');
    expect(send).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenNthCalledWith(1, { source: 'ui', level: 'INFO', message: 'engine socket open' });
    expect(send).toHaveBeenNthCalledWith(2, { source: 'ui', level: 'WARN', message: 'engine socket close: code 1006' });
  });

  it('the error kind is gone: it was a record path production could never fire (E10-05)', () => {
    // The IPC transport carries no error status, so no caller
    // could ever emit 'error' — the map promised a third lifecycle
    // record that did not exist. Trimmed to the two real kinds.
    const send = jest.fn();
    const logger = createUiLogger(send);
    logger.logWsEvent('error', 'boom');
    expect(send).not.toHaveBeenCalled();
  });

  it('forwards the same error again once the dedupe window expires', () => {
    jest.useFakeTimers();
    try {
      const send = jest.fn();
      const logger = createUiLogger(send);
      logger.reportError('kaboom', 'Error: kaboom\n    at x');
      logger.reportError('kaboom', 'Error: kaboom\n    at x');
      expect(send).toHaveBeenCalledTimes(1);
      jest.advanceTimersByTime(1001);
      logger.reportError('kaboom', 'Error: kaboom\n    at x');
      expect(send).toHaveBeenCalledTimes(2);
    } finally {
      jest.useRealTimers();
    }
  });

  it('truncates the ws detail at 300 chars and the error body at 1800', () => {
    const send = jest.fn();
    const logger = createUiLogger(send);
    logger.logWsEvent('close', 'x'.repeat(400));
    expect(send.mock.calls[0][0].message).toBe(`engine socket close: ${'x'.repeat(300)}`);
    logger.reportError('e'.repeat(2500), null);
    expect(send.mock.calls[1][0].message).toHaveLength(1800);
  });

  it('dedupes a repeated error inside the window but not a different one', () => {
    const send = jest.fn();
    const logger = createUiLogger(send);
    logger.reportError('kaboom', 'Error: kaboom\n    at x');
    logger.reportError('kaboom', 'Error: kaboom\n    at x');
    logger.reportError('different', null);
    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[0][0].level).toBe('ERROR');
    expect(send.mock.calls[0][0].message).toContain('kaboom');
  });

  it('is fully inert when send is absent or throws', () => {
    const noSend = createUiLogger(null);
    expect(() => {
      noSend.logWsEvent('open');
      noSend.logMessageType('out', 'MODEL_SET_DEFAULT');
      noSend.reportError('x', null);
    }).not.toThrow();

    const throwing = createUiLogger(() => { throw new Error('bridge gone'); });
    expect(() => throwing.logWsEvent('close')).not.toThrow();
  });
});

describe('installUiLogging', () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('forwards window error events to the logger and stops after uninstall', () => {
    const send = jest.fn();
    const removeSpy = jest.spyOn(window, 'removeEventListener');
    const { uninstall } = installUiLogging(send);
    const err = new Error('render blew up');
    window.dispatchEvent(new ErrorEvent('error', { message: err.message, error: err }));
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toMatchObject({ source: 'ui', level: 'ERROR' });
    expect(send.mock.calls[0][0].message).toContain('render blew up');

    uninstall();
    // uninstall removes exactly the two listeners it added. (A
    // post-uninstall 'error' dispatch is not used for this check:
    // jsdom fails the test on any window error no listener
    // handles, which is the environment proving the point.)
    expect(removeSpy).toHaveBeenCalledWith('error', expect.any(Function));
    expect(removeSpy).toHaveBeenCalledWith('unhandledrejection', expect.any(Function));
    const rejection = new Event('unhandledrejection');
    rejection.reason = new Error('after uninstall');
    window.dispatchEvent(rejection);
    expect(send).toHaveBeenCalledTimes(1);
    removeSpy.mockRestore();
  });

  it('forwards unhandled rejections', () => {
    const send = jest.fn();
    const { uninstall } = installUiLogging(send);
    const event = new Event('unhandledrejection');
    event.reason = new Error('async kaboom');
    window.dispatchEvent(event);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0].message).toContain('async kaboom');
    uninstall();
  });

  it('accepts an existing logger instead of a send function', () => {
    const logger = createUiLogger(jest.fn());
    const reportSpy = jest.spyOn(logger, 'reportError');
    const { logger: returned, uninstall } = installUiLogging(logger);
    expect(returned).toBe(logger);
    window.dispatchEvent(new ErrorEvent('error', { message: 'via logger', error: new Error('via logger') }));
    expect(reportSpy).toHaveBeenCalledTimes(1);
    uninstall();
  });
});
