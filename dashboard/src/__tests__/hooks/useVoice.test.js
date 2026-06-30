import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useVoice } from '../../hooks/useVoice';

function makeMockRecognition() {
  const rec = {
    continuous: false,
    interimResults: false,
    start: vi.fn(),
    stop: vi.fn(),
    onresult: null,
    onerror: null,
    onend: null,
  };
  return rec;
}

describe('useVoice', () => {
  let instances;
  beforeEach(() => {
    instances = [];
    function MockSR() {
      const rec = makeMockRecognition();
      instances.push(rec);
      return rec;
    }
    window.SpeechRecognition = MockSR;
    window.webkitSpeechRecognition = MockSR;
  });
  afterEach(() => {
    delete window.SpeechRecognition;
    delete window.webkitSpeechRecognition;
  });

  it('reports supported when SpeechRecognition exists', () => {
    const { result } = renderHook(() => useVoice());
    expect(result.current.supported).toBe(true);
  });

  it('reports unsupported when neither API is present', () => {
    delete window.SpeechRecognition;
    delete window.webkitSpeechRecognition;
    const { result } = renderHook(() => useVoice());
    expect(result.current.supported).toBe(false);
  });

  it('start() creates a recognition, calls .start(), sets listening', () => {
    const { result } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    expect(instances).toHaveLength(1);
    expect(instances[0].start).toHaveBeenCalled();
    expect(result.current.listening).toBe(true);
  });

  it('start() is a no-op (with error) when unsupported', () => {
    delete window.SpeechRecognition;
    delete window.webkitSpeechRecognition;
    const { result } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    expect(result.current.error).toBe('not supported');
    expect(result.current.listening).toBe(false);
  });

  it('start() catches exception from .start() and surfaces it', () => {
    function ThrowingSR() {
      const rec = makeMockRecognition();
      rec.start = vi.fn(() => { throw new Error('permission denied'); });
      return rec;
    }
    window.SpeechRecognition = ThrowingSR;
    const { result } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    expect(result.current.error).toBe('permission denied');
    expect(result.current.listening).toBe(false);
  });

  it('onresult populates transcript from results', () => {
    const { result } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    const rec = instances[0];
    act(() => {
      rec.onresult({
        results: [[{ transcript: 'hello world' }]],
      });
    });
    expect(result.current.transcript).toBe('hello world');
  });

  it('onerror sets error and stops listening', () => {
    const { result } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    const rec = instances[0];
    act(() => { rec.onerror({ error: 'no-speech' }); });
    expect(result.current.error).toBe('no-speech');
    expect(result.current.listening).toBe(false);
  });

  it('onerror with no .error property falls back to "unknown"', () => {
    const { result } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    const rec = instances[0];
    act(() => { rec.onerror({}); });
    expect(result.current.error).toBe('unknown');
  });

  it('onend flips listening off', () => {
    const { result } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    const rec = instances[0];
    act(() => { rec.onend(); });
    expect(result.current.listening).toBe(false);
  });

  it('stop() calls .stop() on the active recognition', () => {
    const { result } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    const rec = instances[0];
    act(() => { result.current.stop(); });
    expect(rec.stop).toHaveBeenCalled();
  });

  it('stop() swallows errors when no active recognition', () => {
    const { result } = renderHook(() => useVoice());
    expect(() => result.current.stop()).not.toThrow();
  });

  it('unmount stops the active recognition', () => {
    const { result, unmount } = renderHook(() => useVoice());
    act(() => { result.current.start(); });
    const rec = instances[0];
    unmount();
    expect(rec.stop).toHaveBeenCalled();
  });

  it('unmount with no active recognition does not throw', () => {
    const { unmount } = renderHook(() => useVoice());
    expect(() => unmount()).not.toThrow();
  });
});