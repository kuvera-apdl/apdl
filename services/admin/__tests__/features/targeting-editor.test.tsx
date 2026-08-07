import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, test } from 'vitest'

import type { ExperimentTargetingRule } from '../../src/api/types/experiments'
import { SUPPORTED_OPERATORS } from '../../src/core/evaluator/targetingContract'
import {
  ExperimentTargetingRules,
  targetingRulesToFormValues,
  targetingRulesToWire,
  type ExperimentTargetingRuleFormValue,
} from '../../src/features/experiments/ExperimentTargetingRules'
import { TARGETING_OPERATOR_GROUPS } from '../../src/features/targeting/editorModel'

function Harness({ rules }: { rules: ExperimentTargetingRule[] }) {
  const [value, setValue] = useState(() => targetingRulesToFormValues(rules))
  return <ExperimentTargetingRules value={value} onChange={setValue} />
}

const membership = (attribute: string) => ({
  attribute,
  operator: 'in' as const,
  value: ['seed'],
})

describe('shared targeting editor', () => {
  test('pins the one canonical grouped operator set', () => {
    const grouped = TARGETING_OPERATOR_GROUPS.flatMap((group) => group.operators)

    expect(new Set(grouped)).toEqual(SUPPORTED_OPERATORS)
    expect(grouped).toHaveLength(SUPPORTED_OPERATORS.size)
  })

  test('condition insertion preserves the following row draft, error, and focus', () => {
    render(
      <Harness
        rules={[
          {
            id: 'rule_a',
            name: 'First',
            conditions: [
              { attribute: 'plan', operator: 'equals', value: 'pro' },
              membership('cohort'),
            ],
          },
        ]}
      />,
    )

    fireEvent.change(
      screen.getByRole('combobox', { name: 'Targeting rule 1 condition 2 values type' }),
      { target: { value: 'number' } },
    )
    const draft = screen.getByRole('textbox', {
      name: 'Targeting rule 1 condition 2 values',
    }) as HTMLInputElement
    fireEvent.change(draft, { target: { value: 'not-a-number' } })
    fireEvent.click(
      screen.getByRole('button', { name: 'Add targeting rule 1 condition 2 values' }),
    )
    expect(screen.getByText('Enter a finite number or canonical decimal')).toBeInTheDocument()
    draft.focus()

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Add condition after targeting rule 1 condition 1',
      }),
    )

    const movedDraft = screen.getByRole('textbox', {
      name: 'Targeting rule 1 condition 3 values',
    }) as HTMLInputElement
    expect(movedDraft).toBe(draft)
    expect(movedDraft).toHaveValue('not-a-number')
    expect(movedDraft).toHaveFocus()
    expect(screen.getByText('Enter a finite number or canonical decimal')).toBeInTheDocument()
  })

  test('rule reordering preserves nested draft state and focus', () => {
    render(
      <Harness
        rules={[
          { id: 'rule_a', name: 'First', conditions: [membership('cohort')] },
          { id: 'rule_b', name: 'Second', conditions: [membership('country')] },
        ]}
      />,
    )

    const draft = screen.getByRole('textbox', {
      name: 'Targeting rule 1 condition 1 values',
    }) as HTMLInputElement
    fireEvent.change(draft, { target: { value: 'pending' } })
    draft.focus()
    fireEvent.click(screen.getByRole('button', { name: 'Move targeting rule 1 down' }))

    const movedDraft = screen.getByRole('textbox', {
      name: 'Targeting rule 2 condition 1 values',
    }) as HTMLInputElement
    expect(movedDraft).toBe(draft)
    expect(movedDraft).toHaveValue('pending')
    expect(movedDraft).toHaveFocus()
  })

  test('projects typed scalars to the strict wire shape without editor identities', () => {
    const formRules: ExperimentTargetingRuleFormValue[] = [
      {
        id: 'rule_a',
        name: 'Typed',
        conditions: [
          {
            uiId: 'condition_string',
            attribute: 'plan',
            operator: 'equals',
            valueType: 'string',
            value: '18',
            values: [],
          },
          {
            uiId: 'condition_number',
            attribute: 'age',
            operator: 'equals',
            valueType: 'number',
            value: '18',
            values: [],
          },
          {
            uiId: 'condition_boolean',
            attribute: 'beta',
            operator: 'equals',
            valueType: 'boolean',
            value: true,
            values: [],
          },
          {
            uiId: 'condition_membership',
            attribute: 'cohort',
            operator: 'in',
            valueType: 'string',
            value: '',
            values: ['18', 18, true],
          },
          {
            uiId: 'condition_presence',
            attribute: 'email',
            operator: 'exists',
            valueType: 'string',
            value: '',
            values: [],
          },
        ],
      },
    ]

    const wire = targetingRulesToWire(formRules)

    expect(wire[0]?.conditions).toEqual([
      { attribute: 'plan', operator: 'equals', value: '18' },
      { attribute: 'age', operator: 'equals', value: 18 },
      { attribute: 'beta', operator: 'equals', value: true },
      { attribute: 'cohort', operator: 'in', value: ['18', 18, true] },
      { attribute: 'email', operator: 'exists' },
    ])
    expect(JSON.stringify(wire)).not.toMatch(/uiId|valueType|values/)
  })
})
