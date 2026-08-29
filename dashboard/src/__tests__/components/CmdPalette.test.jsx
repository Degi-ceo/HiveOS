import { render, screen, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { CmdPalette } from '../../components/CmdPalette';

// Mock the hooks
vi.mock('../../hooks/useIosKeyboard', () => ({
  useIosKeyboard: () => ({ isKeyboardOpen: false, keyboardHeight: 0 }),
}));
vi.mock('../../hooks/useHaptic', () => ({
  useHaptic: () => ({ trigger: vi.fn() }),
}));

const defaultProps = {
  isOpen: true,
  onClose: vi.fn(),
  query: '',
  setQuery: vi.fn(),
  results: [
    { id: 'cmd:chat',      label: 'Open chat',      group: 'Commands', icon: '💬' },
    { id: 'skill:skill-a', label: 'skill-a',        group: 'Skills',   icon: '⚡' },
    { id: 'cmd:status',    label: 'System status',  group: 'Commands', icon: '🔴' },
    { id: 'memory:m1',    label: 'memory item one', group: 'Memory',   icon: '🧠' },
  ],
  groupMap: {
    Commands: [
      { id: 'cmd:chat',   label: 'Open chat',     group: 'Commands', icon: '💬' },
      { id: 'cmd:status', label: 'System status',  group: 'Commands', icon: '🔴' },
    ],
    Skills: [
      { id: 'skill:skill-a', label: 'skill-a', group: 'Skills', icon: '⚡' },
    ],
    Memory: [
      { id: 'memory:m1', label: 'memory item one', group: 'Memory', icon: '🧠' },
    ],
  },
  selectedIndex: 0,
  setSelectedIndex: vi.fn(),
  selectNext: vi.fn(),
  selectPrev: vi.fn(),
  onExecute: vi.fn(),
};

describe('CmdPalette', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing when isOpen=false', () => {
    render(<CmdPalette {...defaultProps} isOpen={false} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders dialog when isOpen=true', () => {
    render(<CmdPalette {...defaultProps} />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('shows search input with placeholder', () => {
    render(<CmdPalette {...defaultProps} />);
    expect(screen.getByPlaceholderText('What do you need?')).toBeInTheDocument();
  });

  it('shows result groups', () => {
    render(<CmdPalette {...defaultProps} />);
    expect(screen.getByText('Commands')).toBeInTheDocument();
    expect(screen.getByText('Skills')).toBeInTheDocument();
    expect(screen.getByText('Memory')).toBeInTheDocument();
  });

  it('shows result labels', () => {
    render(<CmdPalette {...defaultProps} />);
    expect(screen.getByText('Open chat')).toBeInTheDocument();
    expect(screen.getByText('skill-a')).toBeInTheDocument();
  });

  it('calls setQuery when typing in input', () => {
    render(<CmdPalette {...defaultProps} />);
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'test' } });
    expect(defaultProps.setQuery).toHaveBeenCalledWith('test');
  });

  it('calls onExecute when clicking a result', () => {
    render(<CmdPalette {...defaultProps} />);
    fireEvent.click(screen.getByText('Open chat'));
    expect(defaultProps.onExecute).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'cmd:chat' })
    );
  });

  it('calls onClose when clicking the backdrop', () => {
    render(<CmdPalette {...defaultProps} />);
    // The backdrop div has role="dialog" - click it directly
    const backdrop = screen.getByRole('dialog');
    fireEvent.click(backdrop);
    expect(defaultProps.onClose).toHaveBeenCalled();
  });

  it('does not call onClose when clicking inside the modal', () => {
    render(<CmdPalette {...defaultProps} />);
    // Click the inner modal div (stopPropagation blocks backdrop)
    const modal = document.querySelector('.cmd-palette-modal');
    fireEvent.click(modal);
    expect(defaultProps.onClose).not.toHaveBeenCalled();
  });

  it('shows empty state when no results', () => {
    render(<CmdPalette {...defaultProps} results={[]} groupMap={{}} />);
    expect(screen.getByText('no results')).toBeInTheDocument();
  });

  it('marks selected result with data-selected attribute', () => {
    // Use matching references: same item objects in both results and groupMap
    const item0 = { id: 'cmd:chat', label: 'Open chat', group: 'Commands', icon: '💬' };
    const item1 = { id: 'cmd:status', label: 'System status', group: 'Commands', icon: '🔴' };
    const props = {
      ...defaultProps,
      results: [item0, item1],
      groupMap: { Commands: [item0, item1] },
      selectedIndex: 0,
    };
    render(<CmdPalette {...props} />);
    const options = screen.getAllByRole('option');
    const selectedOption = options.find((o) => o.getAttribute('data-selected') === 'true');
    expect(selectedOption).toBeTruthy();
    expect(selectedOption.textContent).toContain('Open chat');
  });

  it('shows footer hints', () => {
    render(<CmdPalette {...defaultProps} />);
    expect(screen.getByText('↑↓ navigate')).toBeInTheDocument();
    expect(screen.getByText('↵ select')).toBeInTheDocument();
    expect(screen.getByText('esc close')).toBeInTheDocument();
  });

  it('auto-focuses the search input after open', () => {
    render(<CmdPalette {...defaultProps} />);
    act(() => { vi.runAllTimers(); });
    expect(document.activeElement).toBe(screen.getByRole('textbox'));
  });

  it('renders result descriptions when present', () => {
    const withDesc = {
      ...defaultProps,
      results: [{ id: 'memory:m1', label: 'memory item one', group: 'Memory', icon: '🧠', description: 'fact' }],
      groupMap: { Memory: [{ id: 'memory:m1', label: 'memory item one', group: 'Memory', icon: '🧠', description: 'fact' }] },
    };
    render(<CmdPalette {...withDesc} />);
    expect(screen.getByText('fact')).toBeInTheDocument();
  });

  it('calls onClose on Escape keydown', () => {
    render(<CmdPalette {...defaultProps} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(defaultProps.onClose).toHaveBeenCalled();
  });

  it('calls selectNext on ArrowDown', () => {
    render(<CmdPalette {...defaultProps} />);
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(defaultProps.selectNext).toHaveBeenCalled();
  });

  it('calls selectPrev on ArrowUp', () => {
    render(<CmdPalette {...defaultProps} />);
    fireEvent.keyDown(document, { key: 'ArrowUp' });
    expect(defaultProps.selectPrev).toHaveBeenCalled();
  });

  it('calls onExecute on Enter with selected item', () => {
    render(<CmdPalette {...defaultProps} selectedIndex={0} />);
    fireEvent.keyDown(document, { key: 'Enter' });
    expect(defaultProps.onExecute).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'cmd:chat' })
    );
  });
});
