import { useCallback, useMemo } from 'react';

export function useGateway(token) {
  const request = useCallback(async (method, path, body) => {
    const res = await fetch(path, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { 'X-Hive-Token': token } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }, [token]);

  return useMemo(() => ({
    get: (path) => request('GET', path),
    post: (path, body) => request('POST', path, body),
    put: (path, body) => request('PUT', path, body),
    delete: (path) => request('DELETE', path),
  }), [request]);
}