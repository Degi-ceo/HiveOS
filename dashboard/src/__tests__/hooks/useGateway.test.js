import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useGateway } from '../../hooks/useGateway';

describe('useGateway', () => {
  beforeEach(() => {
    global.fetch = vi.fn();
  });

  it('returns a fetch wrapper that adds Authorization header (GET)', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ data: 42 }) });
    const { result } = renderHook(() => useGateway('test-token'));
    const data = await act(() => result.current.get('/foo'));
    expect(data).toEqual({ data: 42 });
    expect(global.fetch).toHaveBeenCalledWith('/foo', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }));
  });

  it('omits Authorization header when no token', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    const { result } = renderHook(() => useGateway(''));
    await act(() => result.current.get('/x'));
    const headers = global.fetch.mock.calls[0][1].headers;
    expect(headers).not.toHaveProperty('Authorization');
    expect(headers['Content-Type']).toBe('application/json');
  });

  it('throws on non-2xx responses with body text', async () => {
    global.fetch.mockResolvedValueOnce({ ok: false, status: 500, text: async () => 'boom' });
    const { result } = renderHook(() => useGateway('t'));
    await expect(result.current.get('/fail')).rejects.toThrow('boom');
  });

  it('POST serializes JSON body', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ ok: 1 }) });
    const { result } = renderHook(() => useGateway('t'));
    await act(() => result.current.post('/chat', { message: 'hi' }));
    expect(global.fetch).toHaveBeenCalledWith('/chat', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ message: 'hi' }),
    }));
  });

  it('GET omits body entirely', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    const { result } = renderHook(() => useGateway('t'));
    await act(() => result.current.get('/x'));
    expect(global.fetch.mock.calls[0][1].body).toBeUndefined();
  });

  it('PUT and DELETE wrappers exist and call fetch with right method', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    const { result } = renderHook(() => useGateway('t'));
    await act(() => result.current.put('/x', { a: 1 }));
    await act(() => result.current.delete('/y'));
    expect(global.fetch.mock.calls[0][1].method).toBe('PUT');
    expect(global.fetch.mock.calls[1][1].method).toBe('DELETE');
  });
});