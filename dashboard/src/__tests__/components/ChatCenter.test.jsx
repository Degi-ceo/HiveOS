import { render, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ChatCenter } from '../../components/ChatCenter';

vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(),
}));

import { useWebSocket } from '../../hooks/useWebSocket';

describe('ChatCenter', () => {
  let fetchMock;
  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    useWebSocket.mockReturnValue({ messages: [], status: 'open' });
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.clearAllMocks();
  });

  it('renders empty state initially', () => {
    const { getByTestId } = render(<ChatCenter token="t" />);
    expect(getByTestId('chat-empty')).toBeInTheDocument();
  });

  it('sends a message via POST /chat when send is clicked', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    const { getByTestId, container } = render(<ChatCenter token="tok" sessionId="s1" />);
    const input = getByTestId('chat-input');
    fireEvent.change(input, { target: { value: 'hello' } });
    getByTestId('chat-send').click();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('/chat');
    expect(opts.method).toBe('POST');
    const body = JSON.parse(opts.body);
    expect(body.message).toBe('hello');
    expect(body.session_id).toBe('s1');
    expect(opts.headers['X-Hive-Token']).toBe('tok');
    await waitFor(() => {
      const userMsg = container.querySelector('[data-role="user"]');
      expect(userMsg).not.toBeNull();
      expect(userMsg.textContent).toContain('hello');
    });
  });

  it('accumulates streaming tokens into the last assistant message', () => {
    useWebSocket.mockReturnValue({
      messages: [
        { event_type: 'token', text: 'Hello' },
        { event_type: 'token', text: ' world' },
        { event_type: 'token', text: '!' },
      ],
      status: 'open',
    });
    const { container } = render(<ChatCenter token="t" />);
    const assistant = container.querySelector('[data-role="assistant"]');
    expect(assistant).not.toBeNull();
    expect(assistant.textContent).toContain('Hello world!');
  });

  it('renders tool_call events as a tool chip', () => {
    useWebSocket.mockReturnValue({
      messages: [{ event_type: 'tool_call', tool_name: 'web_search', args: { q: 'x' } }],
      status: 'open',
    });
    const { container } = render(<ChatCenter token="t" />);
    const tool = container.querySelector('[data-role="tool"]');
    expect(tool).not.toBeNull();
    expect(tool.textContent).toContain('web_search');
  });

  it('invokes onApproval when an approval_request message arrives', () => {
    const onApproval = vi.fn();
    useWebSocket.mockReturnValue({
      messages: [{ event_type: 'approval_request', id: 'a1', tool: 'web_get' }],
      status: 'open',
    });
    render(<ChatCenter token="t" onApproval={onApproval} />);
    expect(onApproval).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'a1', tool: 'web_get' })
    );
  });

  it('does nothing when input is empty', async () => {
    const { getByTestId } = render(<ChatCenter token="t" />);
    getByTestId('chat-send').click();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows error message when POST /chat fails', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, text: async () => 'nope' });
    const { getByTestId, container } = render(<ChatCenter token="t" />);
    const input = getByTestId('chat-input');
    fireEvent.change(input, { target: { value: 'hi' } });
    getByTestId('chat-send').click();
    await waitFor(() => {
      const err = container.querySelector('[data-role="error"]');
      expect(err).not.toBeNull();
    });
  });

  it('applies voiceTranscript to the input', () => {
    const { getByTestId, rerender } = render(<ChatCenter token="t" voiceTranscript="" />);
    rerender(<ChatCenter token="t" voiceTranscript="speak this" />);
    const input = getByTestId('chat-input');
    expect(input.value).toBe('speak this');
  });
});
