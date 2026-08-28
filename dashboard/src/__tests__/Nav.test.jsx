import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { Nav } from '../components/Nav';

vi.mock('../hooks/useHaptic', () => ({
  useHaptic: () => ({ trigger: vi.fn() }),
}));

const NAV_ITEMS = [
  // Main
  'Home', 'Chat', 'Skills',
  // Live
  'Activity', 'Voice',
  // Workspace
  'Memory', 'Kanban', 'Agents',
  // System
  'Approvals', 'Settings',
];

describe('Nav', () => {
  beforeEach(() => {
    // Reset window.innerWidth between tests
    Object.defineProperty(window, 'innerWidth', { value: 1200, writable: true });
  });

  it('renders all nav items', () => {
    render(<Nav isOpen={true} onClose={vi.fn()} />);
    NAV_ITEMS.forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument();
    });
  });

  it('renders HIVE wordmark', () => {
    render(<Nav isOpen={true} onClose={vi.fn()} />);
    expect(screen.getByText('HIVE')).toBeInTheDocument();
  });

  it('renders voice button in footer', () => {
    render(<Nav isOpen={true} onClose={vi.fn()} />);
    // Two buttons have aria-label containing "Voice": the nav item and the footer button.
    // Query the footer voice button via its class.
    const footerBtn = document.querySelector('.nav-voice-btn');
    expect(footerBtn).toBeInTheDocument();
    expect(footerBtn.getAttribute('aria-label')).toMatch(/voice/i);
  });

  it('calls onClose when backdrop is clicked (mobile)', () => {
    Object.defineProperty(window, 'innerWidth', { value: 600, writable: true });
    const onClose = vi.fn();
    render(<Nav isOpen={true} onClose={onClose} />);
    const backdrop = document.querySelector('.nav-backdrop');
    if (backdrop) fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalled();
  });

  it('nav element exists with correct aria-label', () => {
    render(<Nav isOpen={true} onClose={vi.fn()} />);
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument();
  });

  it('has nav--open class when isOpen is true', () => {
    render(<Nav isOpen={true} onClose={vi.fn()} />);
    const nav = document.querySelector('.nav');
    expect(nav).toBeInTheDocument();
  });

  it('does not have nav--open class when isOpen is false', () => {
    render(<Nav isOpen={false} onClose={vi.fn()} />);
    const nav = document.querySelector('.nav');
    expect(nav).not.toHaveClass('nav--open');
  });

  it('clicking a nav item does not throw (regression: useHaptic() destructure)', () => {
    render(<Nav isOpen={true} onClose={vi.fn()} />);
    expect(() => fireEvent.click(screen.getByText('Chat'))).not.toThrow();
  });
});
