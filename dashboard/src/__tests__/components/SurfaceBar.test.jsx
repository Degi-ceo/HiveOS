import { render, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { SurfaceBar } from '../../components/SurfaceBar';

describe('SurfaceBar', () => {
  beforeEach(() => {
    global.fetch = vi.fn();
  });

  it('renders 4 channel pills', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ channels: {} }),
    });
    const { container } = render(<SurfaceBar token="t" />);
    const pills = container.querySelectorAll('.surface-bar > span');
    expect(pills).toHaveLength(4);
    expect(pills[0].dataset.channel).toBe('telegram');
    expect(pills[1].dataset.channel).toBe('slack');
    expect(pills[2].dataset.channel).toBe('discord');
    expect(pills[3].dataset.channel).toBe('email');
  });

  it('marks active channels based on /health/summary response', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ channels: { telegram: true, slack: false, discord: true, email: true } }),
    });
    const { container } = render(<SurfaceBar token="t" />);
    await waitFor(() => {
      expect(container.querySelector('[data-channel="telegram"]').dataset.active).toBe('true');
      expect(container.querySelector('[data-channel="slack"]').dataset.active).toBe('false');
      expect(container.querySelector('[data-channel="discord"]').dataset.active).toBe('true');
      expect(container.querySelector('[data-channel="email"]').dataset.active).toBe('true');
    });
  });

  it('handles missing channels key gracefully', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({}),
    });
    const { container } = render(<SurfaceBar token="t" />);
    await waitFor(() => {
      const pills = container.querySelectorAll('.surface-bar > span');
      pills.forEach((p) => expect(p.dataset.active).toBe('false'));
    });
  });

  it('handles fetch failure by leaving channels inactive', async () => {
    global.fetch.mockRejectedValueOnce(new Error('boom'));
    const { container } = render(<SurfaceBar token="t" />);
    await waitFor(() => {
      const pills = container.querySelectorAll('.surface-bar > span');
      pills.forEach((p) => expect(p.dataset.active).toBe('false'));
    });
  });
});