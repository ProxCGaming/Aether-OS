/** Full Furnish Spec C (FF3-07): the ONE shared model picker.
 * Chat, Settings default, aux and per-agent pickers all render
 * ModelPicker over the shared Dropdown core. These tests pin the
 * option-building contract per format (values stay exactly what
 * each call site always wrote) and the metadata rendering rules:
 * engine-known data shows, unknown data is absent, and a
 * learned-unusable model is badged — never dropped, never blocked. */
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import ModelPicker, {
  buildModelOptions,
  modelMetaLine,
  relativeCostLabel,
} from '../ModelPicker';

const PROVIDERS = [
  {
    name: 'openai',
    display_name: 'OpenAI',
    has_key: true,
    is_default: true,
    default_model: 'gpt-4o',
    models: ['gpt-4o', 'mystery-model'],
    model_meta: {
      'gpt-4o': {
        capabilities: ['chat', 'code', 'reasoning', 'tools', 'vision'],
        cost_input_1m: 2.5,
        cost_output_1m: 10.0,
        tier: 'strong',
      },
    },
  },
  {
    name: 'google_gemini',
    display_name: 'Google Gemini',
    has_key: true,
    is_default: false,
    default_model: 'gemini-2.5-flash',
    models: ['gemini-2.5-flash'],
    model_meta: {
      'gemini-2.5-flash': { unusable: true },
    },
  },
  {
    name: 'keyless',
    display_name: 'Keyless',
    has_key: false,
    is_default: false,
    default_model: '',
    models: ['some-model'],
  },
];

describe('buildModelOptions', () => {
  test('grouped format keeps the chat/default/aux contract: provider groups, ::: values, key+models filter', () => {
    const options = buildModelOptions(PROVIDERS, 'grouped');
    expect(options.map(o => o.group)).toEqual(['OpenAI', 'Google Gemini']);
    expect(options[0].items.map(i => i.value)).toEqual(['openai:::gpt-4o', 'openai:::mystery-model']);
    expect(options[0].items.map(i => i.label)).toEqual(['gpt-4o', 'mystery-model']);
  });

  test('agent format keeps the per-agent contract: flat list, single-colon values, provider-suffixed labels, no key filter', () => {
    const options = buildModelOptions(PROVIDERS, 'agent');
    expect(options.map(o => o.value)).toEqual([
      'openai:gpt-4o',
      'openai:mystery-model',
      'google_gemini:gemini-2.5-flash',
      'keyless:some-model',
    ]);
    expect(options[0].label).toBe('gpt-4o (OpenAI)');
  });

  test('metadata attaches only where the engine supplied it', () => {
    const options = buildModelOptions(PROVIDERS, 'grouped');
    const [gpt4o, mystery] = options[0].items;
    expect(gpt4o.meta).toBe('Vision · Tools · $$');
    expect(gpt4o.badge).toBeUndefined();
    // No model_meta entry: bare row, nothing invented.
    expect(mystery.meta).toBeUndefined();
    expect(mystery.badge).toBeUndefined();
    const gemini = options[1].items[0];
    expect(gemini.badge).toEqual({ text: 'Unusable', tone: 'danger' });
  });
});

describe('metadata formatting', () => {
  test('relative cost bands derive from the cached prices only', () => {
    expect(relativeCostLabel(null)).toBeNull();
    expect(relativeCostLabel({})).toBeNull();
    expect(relativeCostLabel({ cost_input_1m: 0, cost_output_1m: 0 })).toBe('Free');
    expect(relativeCostLabel({ cost_input_1m: 0.15, cost_output_1m: 0.6 })).toBe('$');
    expect(relativeCostLabel({ cost_input_1m: 2.5, cost_output_1m: 10 })).toBe('$$');
    expect(relativeCostLabel({ cost_input_1m: 15, cost_output_1m: 60 })).toBe('$$$');
  });

  test('the meta line shows only the parts that exist', () => {
    expect(modelMetaLine(undefined)).toBeUndefined();
    expect(modelMetaLine({ capabilities: ['chat'] })).toBeUndefined();
    expect(modelMetaLine({ capabilities: ['vision'] })).toBe('Vision');
    expect(modelMetaLine({ capabilities: ['tools'], cost_input_1m: 0, cost_output_1m: 0 })).toBe('Tools · Free');
  });
});

describe('ModelPicker rendering', () => {
  test('rows show the meta line and the Unusable badge; the badged model stays listed and selectable', () => {
    const onChange = jest.fn();
    render(<ModelPicker providers={PROVIDERS} value="" onChange={onChange} testId="mp" placeholder="Pick" />);
    fireEvent.click(screen.getByText('Pick'));
    expect(screen.getByText('Vision · Tools · $$')).toBeInTheDocument();
    expect(screen.getByText('Unusable')).toBeInTheDocument();
    // Badged, not dropped and not blocked: selecting writes the
    // same value the old raw picker wrote.
    fireEvent.click(screen.getByText('gemini-2.5-flash'));
    expect(onChange).toHaveBeenCalledWith('google_gemini:::gemini-2.5-flash');
  });

  test('search narrows the list and a fruitless search gets the no-results treatment', () => {
    render(<ModelPicker providers={PROVIDERS} value="" onChange={jest.fn()} testId="mp" placeholder="Pick" />);
    fireEvent.click(screen.getByText('Pick'));
    const search = screen.getByTestId('mp-search');
    fireEvent.change(search, { target: { value: 'mystery' } });
    expect(screen.getByText('mystery-model')).toBeInTheDocument();
    expect(screen.queryByText('gpt-4o')).not.toBeInTheDocument();
    fireEvent.change(search, { target: { value: 'zzz' } });
    expect(screen.getByTestId('mp-no-results')).toBeInTheDocument();
    expect(screen.getByText('No matching models')).toBeInTheDocument();
  });

  test('leading options (auto / inherit) render ahead of the provider options', () => {
    render(
      <ModelPicker
        providers={PROVIDERS}
        value="auto"
        onChange={jest.fn()}
        leadingOptions={[{ value: 'auto', label: 'Use main model' }]}
        testId="mp"
      />
    );
    // The leading option is the current value, so it is the
    // trigger's selected label.
    expect(within(screen.getByTestId('mp')).getByText('Use main model')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Use main model'));
    expect(screen.getByText('gpt-4o')).toBeInTheDocument();
  });
});
