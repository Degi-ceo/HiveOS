import React from 'react';
import { useGateway } from '../hooks/useGateway';

export function ApprovalModal({ token, request, onClose }) {
  const { post } = useGateway(token);
  if (!request) return null;
  const decide = async (approve) => {
    try {
      await post(`/approvals/${request.id}/${approve ? 'approve' : 'reject'}`);
    } finally {
      onClose?.();
    }
  };
  return (
    <div className="modal-backdrop" data-testid="approval-modal">
      <div className="glass modal">
        <h3>approval: {request.tool || request.action || request.id}</h3>
        <pre data-testid="approval-args">{JSON.stringify(request.args || {}, null, 2)}</pre>
        <div className="modal__actions">
          <button data-testid="approve" onClick={() => decide(true)}>approve</button>
          <button data-testid="reject" onClick={() => decide(false)}>reject</button>
        </div>
      </div>
    </div>
  );
}

export default ApprovalModal;
