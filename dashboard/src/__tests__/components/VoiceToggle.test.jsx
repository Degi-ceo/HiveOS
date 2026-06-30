import { render, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { VoiceToggle } from '../../components/VoiceToggle';

vi.mock('../../hooks/useVoice', () => ({
  useVoice: vi.fn(),
}));

import { useVoice } from '../../hooks/useVoice';

describe('VoiceToggle', () => {
  beforeEach(() => {
    useVoice.mockReturnValue({
      supported: true, listening: false, transcript: '',
      start: vi.fn(), stop: vi.fn(), error: null,
    });
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('renders the mic button when supported', () => {
    const { getByTestId } = render(<VoiceToggle />);
    const btn = getByTestId('voice-toggle');
    expect(btn).toBeInTheDocument();
    expect(btn.getAttribute('data-listening')).toBe('false');
  });

  it('renders unsupported text when API not available', () => {
    useVoice.mockReturnValue({
      supported: false, listening: false, transcript: '',
      start: vi.fn(), stop: vi.fn(), error: null,
    });
    const { getByTestId, queryByTestId } = render(<VoiceToggle />);
    expect(getByTestId('voice-unsupported')).toBeInTheDocument();
    expect(queryByTestId('voice-toggle')).toBeNull();
  });

  it('clicking the button calls start when not listening', () => {
    const start = vi.fn();
    const stop = vi.fn();
    useVoice.mockReturnValue({
      supported: true, listening: false, transcript: '',
      start, stop, error: null,
    });
    const { getByTestId } = render(<VoiceToggle />);
    getByTestId('voice-toggle').click();
    expect(start).toHaveBeenCalled();
    expect(stop).not.toHaveBeenCalled();
  });

  it('clicking the button calls stop when listening', () => {
    const start = vi.fn();
    const stop = vi.fn();
    useVoice.mockReturnValue({
      supported: true, listening: true, transcript: '',
      start, stop, error: null,
    });
    const { getByTestId } = render(<VoiceToggle />);
    getByTestId('voice-toggle').click();
    expect(stop).toHaveBeenCalled();
    expect(start).not.toHaveBeenCalled();
  });

  it('passes transcript to onTranscript when transcript changes', () => {
    const onTranscript = vi.fn();
    useVoice.mockReturnValue({
      supported: true, listening: true, transcript: 'hello hive',
      start: vi.fn(), stop: vi.fn(), error: null,
    });
    render(<VoiceToggle onTranscript={onTranscript} />);
    expect(onTranscript).toHaveBeenCalledWith('hello hive');
  });
});
