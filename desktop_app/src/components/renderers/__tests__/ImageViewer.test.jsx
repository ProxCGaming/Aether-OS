import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ImageViewer from '../ImageViewer';

// ImageViewer: an artifact image with a zoom toolbar — in/out in
// 25% steps clamped to [25%, 400%], a reset back to 100%, and the
// current level always shown and applied to the image.

const PNG_SRC =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

describe('ImageViewer', () => {
  it('renders the image with the given src and alt at 100% zoom', () => {
    render(<ImageViewer src={PNG_SRC} alt="Test image" />);
    expect(screen.getByTestId('image-viewer')).toBeInTheDocument();
    const img = screen.getByAltText('Test image');
    expect(img).toHaveAttribute('src', PNG_SRC);
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('100%');
    expect(img).toHaveStyle({ transform: 'scale(1)' });
  });

  it('falls back to the default alt text when none is given', () => {
    render(<ImageViewer src={PNG_SRC} />);
    expect(screen.getByAltText('Artifact Image')).toBeInTheDocument();
  });

  it('zoom in steps the level up by 25% and scales the image', () => {
    render(<ImageViewer src={PNG_SRC} alt="Zoomable" />);
    fireEvent.click(screen.getByTestId('zoom-in'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('125%');
    expect(screen.getByAltText('Zoomable')).toHaveStyle({ transform: 'scale(1.25)' });
    fireEvent.click(screen.getByTestId('zoom-in'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('150%');
  });

  it('zoom out steps back down and never goes below 25%', () => {
    render(<ImageViewer src={PNG_SRC} alt="Zoomable" />);
    fireEvent.click(screen.getByTestId('zoom-out'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('75%');
    fireEvent.click(screen.getByTestId('zoom-out'));
    fireEvent.click(screen.getByTestId('zoom-out'));
    fireEvent.click(screen.getByTestId('zoom-out'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('25%');
  });

  it('zoom in never goes above 400%', () => {
    render(<ImageViewer src={PNG_SRC} alt="Zoomable" />);
    for (let i = 0; i < 14; i += 1) {
      fireEvent.click(screen.getByTestId('zoom-in'));
    }
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('400%');
  });

  it('reset restores 100% after zooming', () => {
    render(<ImageViewer src={PNG_SRC} alt="Zoomable" />);
    fireEvent.click(screen.getByTestId('zoom-in'));
    fireEvent.click(screen.getByTestId('zoom-in'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('150%');
    fireEvent.click(screen.getByTestId('zoom-reset'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('100%');
    expect(screen.getByAltText('Zoomable')).toHaveStyle({ transform: 'scale(1)' });
  });
});
