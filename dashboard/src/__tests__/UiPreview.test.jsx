import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { UiPreview } from '../ui-preview/UiPreview';
import { screens } from '../ui-preview/screenCatalog';

describe('UiPreview', () => {
  it('registers every canonical top-level HiveOS page', () => {
    const required = [
      'hub', 'chat', 'memory', 'skills', 'files', 'agents', 'tasks', 'channels',
      'mcp', 'logs', 'activity', 'sessions', 'approvals', 'self-improve',
      'analytics', 'docs', 'settings',
    ];
    expect(required.every((id) => screens[id])).toBe(true);
  });

  it('switches between isolated placeholder views', () => {
    window.history.replaceState({}, '', '/?ui-preview=1');
    const { getByRole, getByText } = render(<UiPreview />);
    expect(getByText('A complete view of HiveOS right now')).toBeInTheDocument();
    fireEvent.click(getByRole('button', { name: 'Memory' }));
    expect(getByText('Knowledge retained across agents')).toBeInTheDocument();
    expect(getByText('/memory/stats')).toBeInTheDocument();
  });
});
