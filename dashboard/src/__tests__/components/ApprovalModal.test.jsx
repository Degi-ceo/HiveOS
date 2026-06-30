import { render, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApprovalModal } from '../../components/ApprovalModal';

describe('ApprovalModal', () => {
  let fetchMock;
  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock;
  });
  afterEach(() => {
    fetchMock.mockReset();
  });

  it('returns null when request is null', () => {
    const { container } = render(<ApprovalModal token="t" request={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders args from the request', () => {
    const req = { id: 'a1', tool: 'web_get', args: { url: 'https://example.com' } };
    const { getByTestId, container } = render(<ApprovalModal token="t" request={req} />);
    expect(container.textContent).toContain('web_get');
    const args = getByTestId('approval-args');
    expect(args.textContent).toContain('https://example.com');
  });

  it('clicking approve POSTs to /approvals/{id}/approve and calls onClose', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    const onClose = vi.fn();
    const { getByTestId } = render(
      <ApprovalModal token="tok" request={{ id: 'a1', tool: 'web_get', args: {} }} onClose={onClose} />
    );
    getByTestId('approve').click();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toContain('/approvals/a1/approve');
    expect(opts.method).toBe('POST');
    expect(opts.headers.Authorization).toBe('Bearer tok');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('clicking reject POSTs to /approvals/{id}/reject and calls onClose', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    const onClose = vi.fn();
    const { getByTestId } = render(
      <ApprovalModal token="t" request={{ id: 'b2', tool: 'shell', args: {} }} onClose={onClose} />
    );
    getByTestId('reject').click();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url] = fetchMock.mock.calls[0];
    expect(url).toContain('/approvals/b2/reject');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});
