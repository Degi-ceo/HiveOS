import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { StatusOrb } from '../../components/StatusOrb';

describe('StatusOrb', () => {
  it('renders with default state="idle"', () => {
    render(<StatusOrb />);
    const el = document.querySelector('.orb');
    expect(el).toBeInTheDocument();
    expect(el.dataset.state).toBe('idle');
    expect(el.title).toBe('idle');
  });

  it('renders warn class for state="warn"', () => {
    render(<StatusOrb state="warn" />);
    const el = document.querySelector('.orb.warn');
    expect(el).toBeInTheDocument();
    expect(el.title).toBe('warning');
  });

  it('renders error class for state="error"', () => {
    render(<StatusOrb state="error" />);
    const el = document.querySelector('.orb.error');
    expect(el).toBeInTheDocument();
    expect(el.title).toBe('error');
  });

  it('renders plain .orb for ok/working/idle', () => {
    const { rerender } = render(<StatusOrb state="ok" />);
    expect(document.querySelector('.orb')).toBeInTheDocument();
    expect(document.querySelector('.orb.error')).toBeNull();
    rerender(<StatusOrb state="working" />);
    expect(document.querySelector('.orb')).toBeInTheDocument();
  });

  it('falls back to state name for unknown states', () => {
    render(<StatusOrb state="xyz" />);
    const el = document.querySelector('.orb');
    expect(el.title).toBe('xyz');
  });
});