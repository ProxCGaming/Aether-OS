/** Wave 5 (D4-6): the skill install card — the pin summary the
 * user approves before a registry install writes anything. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import SkillInstallCard from '../SkillInstallCard';

const PIN = {
  name: 'hello-skill',
  description: 'Says hello',
  version: '1.2',
  source_type: 'local',
  source: '/home/user/skill-catalogue',
  hash: 'a'.repeat(64),
};

describe('SkillInstallCard', () => {
  test('shows the source, version and full pin hash', () => {
    render(<SkillInstallCard pin={PIN} onConfirm={jest.fn()} onCancel={jest.fn()} />);
    expect(screen.getByText(/Install skill “hello-skill”\?/)).toBeInTheDocument();
    expect(screen.getByText(/Local directory — \/home\/user\/skill-catalogue/)).toBeInTheDocument();
    expect(screen.getByText('Version: 1.2')).toBeInTheDocument();
    expect(screen.getByText('a'.repeat(64))).toBeInTheDocument();
  });

  test('states the disabled landing before the user commits', () => {
    render(<SkillInstallCard pin={PIN} onConfirm={jest.fn()} onCancel={jest.fn()} />);
    expect(screen.getByText(/It will be installed disabled/)).toBeInTheDocument();
  });

  test('confirm and cancel fire their handlers', () => {
    const onConfirm = jest.fn();
    const onCancel = jest.fn();
    render(<SkillInstallCard pin={PIN} onConfirm={onConfirm} onCancel={onCancel} />);
    fireEvent.click(screen.getByText('Install'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  test('renders nothing without a pin', () => {
    const { container } = render(
      <SkillInstallCard pin={null} onConfirm={jest.fn()} onCancel={jest.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
